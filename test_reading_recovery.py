"""Run both reading scripts with fake configuration, HTTP, clock and push channels.

No credentials, external requests or real sleeps are used.
Run with: python -m unittest -v test_reading_recovery
"""
import json
import logging
from pathlib import Path
import runpy
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


class RequestError(Exception):
    pass


class Response:
    def __init__(self, payload=None, status=200, cookies=None, invalid_json=False):
        self.payload = payload
        self.status_code = status
        self.cookies = cookies or {}
        self.invalid_json = invalid_json

    def json(self):
        if self.invalid_json:
            raise ValueError('invalid JSON')
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RequestError(f'HTTP {self.status_code}')


SUCCESS = {'succ': 1, 'synckey': 123}


class ReadingRecoveryTests(unittest.TestCase):
    scripts = ('main.py', 'main_v2.py')

    def run_script(self, script, reads, renewals=None, repairs=None, varied_timing=False):
        config = ModuleType('config')
        config.data = {'s': 'initial', 'appId': 'test', 'b': 'test', 'c': 'test'}
        config.headers = {'user-agent': 'test'}
        config.cookies = {'wr_skey': 'existing'}
        config.READ_NUM = 2
        config.PUSH_METHOD = 'test'
        config.book = ['test-book']
        config.chapter = ['test-chapter']
        config.READ_DURATION_MINUTES_MIN = config.READ_DURATION_MINUTES_MAX = 1
        config.READ_INTERVAL_SECONDS_MIN = config.READ_INTERVAL_SECONDS_MAX = 30
        if varied_timing:
            config.READ_DURATION_MINUTES_MAX = 2
            config.READ_INTERVAL_SECONDS_MIN = 25
            config.READ_INTERVAL_SECONDS_MAX = 35
        push_module = ModuleType('push')
        push_module.push = Mock()
        log_module = ModuleType('log_utils')
        log_module.setup_logging = Mock(return_value=Mock())
        clock = ModuleType('time')
        clock.now = 1000
        clock.time = lambda: clock.now
        clock.sleeps = []

        def sleep(seconds):
            clock.sleeps.append(seconds)
            clock.now += seconds

        clock.sleep = sleep
        random_module = ModuleType('random')
        intervals = iter([25, 35, 25, 35])
        random_module.uniform = Mock(side_effect=lambda low, high: 65 if varied_timing else low)
        random_module.randint = Mock(side_effect=lambda low, high: next(intervals) if varied_timing and (low, high) == (25, 35) else low)
        random_module.choice = Mock(side_effect=lambda values: values[0])
        request_module = ModuleType('requests')
        request_module.RequestException = RequestError
        request_module.calls = []
        reads = iter(reads)
        renewals = iter(renewals) if renewals is not None else None
        repairs = iter(repairs) if repairs is not None else None

        def post(url, **kwargs):
            request_module.calls.append((url, kwargs.copy()))
            if len(request_module.calls) > 50:
                raise AssertionError('Reading recovery did not terminate')
            self.assertEqual(kwargs.get('timeout'), (10, 30))
            if url.endswith('/read'):
                item = next(reads)
            elif url.endswith('/renewal'):
                item = next(renewals) if renewals is not None else Response(cookies={'wr_skey': 'abcdefgh-new'})
            else:
                self.assertTrue(url.endswith('/chapterInfos'))
                item = next(repairs) if repairs is not None else Response()
            if isinstance(item, Exception):
                raise item
            return item if isinstance(item, Response) else Response(item)

        request_module.post = post
        result = None
        error = None
        modules = {'config': config, 'requests': request_module, 'push': push_module,
                   'log_utils': log_module, 'time': clock, 'random': random_module}
        with patch.dict(sys.modules, modules), self.assertLogs(level=logging.INFO) as captured:
            try:
                result = runpy.run_path(str(Path(__file__).parent / script))
            except Exception as exc:
                error = exc
        return result, error, request_module.calls, clock.sleeps, push_module.push, captured.output, config

    def assert_failure_push(self, push, text):
        push.assert_called_once()
        self.assertIn(text, push.call_args.args[0])
        self.assertEqual(push.call_args.kwargs, {'is_success': False})

    def test_startup_renewal_failure_preserves_cookie_and_reads(self):
        for script in self.scripts:
            for renewals in ([Response()] * 3, [RequestError('timeout')] * 3):
                with self.subTest(script=script, network_failure=isinstance(renewals[0], Exception)):
                    result, error, calls, sleeps, push, logs, config = self.run_script(script, [SUCCESS] * 2, renewals)
                    self.assertIsNone(error)
                    self.assertEqual(config.cookies['wr_skey'], 'existing')
                    self.assertEqual(sum(url.endswith('/read') for url, _ in calls), 2)
                    self.assertTrue(push.call_args.kwargs['is_success'])
                    self.assertTrue(any('保留现有 cookie' in line for line in logs))
                    self.assertEqual(sleeps, [30, 30] if script == 'main.py' else [30])
                    if script == 'main_v2.py':
                        self.assertEqual(result['read_elapsed_seconds'], 60)

    def test_read_network_error_notifies_and_stops(self):
        for script in self.scripts:
            with self.subTest(script=script):
                _, error, _, sleeps, push, _, _ = self.run_script(script, [RequestError('connection timed out')])
                self.assertIsInstance(error, RuntimeError)
                self.assertIsInstance(error.__cause__, RequestError)
                self.assertEqual(sleeps, [])
                self.assert_failure_push(push, 'connection timed out')

    def test_non_json_response_reports_http_status(self):
        for script in self.scripts:
            with self.subTest(script=script):
                _, error, _, _, push, _, _ = self.run_script(script, [Response(status=503, invalid_json=True)])
                self.assertIsInstance(error, RuntimeError)
                self.assert_failure_push(push, 'HTTP 503')

    def test_missing_synckey_stops_after_three_repairs(self):
        for script in self.scripts:
            with self.subTest(script=script):
                _, error, calls, sleeps, push, _, _ = self.run_script(script, [{'succ': 1}] * 4)
                self.assertIsInstance(error, RuntimeError)
                self.assertEqual(sum(url.endswith('/chapterInfos') for url, _ in calls), 3)
                self.assertEqual(sleeps, [5, 5, 5])
                self.assert_failure_push(push, '连续 3 次未恢复 synckey')

    def test_repair_failure_notifies_and_stops(self):
        for script in self.scripts:
            for repair in (Response(status=500), RequestError('repair timed out')):
                with self.subTest(script=script, repair=type(repair).__name__):
                    _, error, _, sleeps, push, _, _ = self.run_script(script, [{'succ': 1}], repairs=[repair])
                    self.assertIsInstance(error, RuntimeError)
                    self.assertEqual(sleeps, [])
                    self.assert_failure_push(push, 'synckey 修复请求失败')

    def test_success_resets_consecutive_repair_counter(self):
        reads = [{'succ': 1}] * 3 + [SUCCESS] + [{'succ': 1}] * 3 + [SUCCESS]
        for script in self.scripts:
            with self.subTest(script=script):
                result, error, calls, sleeps, push, _, _ = self.run_script(script, reads)
                self.assertIsNone(error)
                self.assertEqual(sum(url.endswith('/chapterInfos') for url, _ in calls), 6)
                self.assertEqual(sleeps.count(5), 6)
                self.assertTrue(push.call_args.kwargs['is_success'])
                if script == 'main_v2.py':
                    self.assertEqual(result['read_elapsed_seconds'], 60)
                    self.assertEqual(result['index'], 3)

    def test_api_error_keeps_diagnostics_and_recovers(self):
        for script in self.scripts:
            for code in (-2012, -2013, -9999):
                with self.subTest(script=script, code=code):
                    reads = [Response({'errCode': code, 'errMsg': 'test server error'}, status=403), SUCCESS, SUCCESS]
                    _, error, calls, _, push, logs, _ = self.run_script(script, reads)
                    self.assertIsNone(error)
                    self.assertEqual(sum(url.endswith('/renewal') for url, _ in calls), 2)
                    self.assertTrue(any(f'HTTP 403，errCode={code}，errMsg=test server error' in line for line in logs))
                    expected = '检测到登录/鉴权异常' if code in (-2012, -2013) else '未识别为已知鉴权错误'
                    self.assertTrue(any(expected in line for line in logs))
                    self.assertTrue(push.call_args.kwargs['is_success'])

    def test_failed_renewal_after_read_error_is_fatal(self):
        for script in self.scripts:
            with self.subTest(script=script):
                renewals = [Response(cookies={'wr_skey': 'abcdefgh'})] + [Response()] * 3
                _, error, _, _, push, _, _ = self.run_script(script, [{'errCode': -2012, 'errMsg': 'expired'}], renewals)
                self.assertIsNotNone(error)
                self.assert_failure_push(push, '当前登录状态可能已失效')

    def test_successful_read_payload_still_has_signature_and_timing(self):
        for script in self.scripts:
            with self.subTest(script=script):
                _, error, calls, _, _, _, _ = self.run_script(script, [SUCCESS, SUCCESS])
                self.assertIsNone(error)
                payloads = [json.loads(kwargs['data']) for url, kwargs in calls if url.endswith('/read')]
                self.assertEqual(len(payloads), 2)
                for payload in payloads:
                    self.assertEqual(payload['rt'], 30)
                    self.assertEqual(payload['b'], 'test-book')
                    self.assertEqual(payload['c'], 'test-chapter')
                    self.assertEqual(len(payload['sg']), 64)
                    self.assertTrue(payload['s'])

    def test_random_duration_and_variable_intervals_are_preserved(self):
        result, error, calls, sleeps, push, _, _ = self.run_script(
            'main_v2.py', [SUCCESS] * 3, varied_timing=True,
        )
        self.assertIsNone(error)
        self.assertEqual(result['target_duration_seconds'], 65)
        self.assertEqual(result['read_elapsed_seconds'], 95)
        self.assertEqual(sleeps, [35, 25])
        payloads = [json.loads(kwargs['data']) for url, kwargs in calls if url.endswith('/read')]
        self.assertEqual([payload['rt'] for payload in payloads], [25, 35, 25])
        self.assertTrue(push.call_args.kwargs['is_success'])


if __name__ == '__main__':
    unittest.main()
