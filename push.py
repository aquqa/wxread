import json
import logging
import os
import random
import smtplib
import ssl
import time
from email.message import EmailMessage

import requests

from config import (
    EMAIL_AUTH_CODE,
    EMAIL_ENABLED,
    EMAIL_RECIPIENTS,
    EMAIL_SENDER,
    EMAIL_SMTP_HOST,
    EMAIL_SMTP_PORT,
    EMAIL_SUBJECT_PREFIX,
    PUSHPLUS_TOKEN,
    SERVERCHAN_SPT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WXPUSHER_SPT,
)

logger = logging.getLogger(__name__)


class PushNotification:
    def __init__(self):
        self.pushplus_url = "https://www.pushplus.plus/send"
        self.telegram_url = "https://api.telegram.org/bot{}/sendMessage"
        self.server_chan_url = "https://sctapi.ftqq.com/{}.send"
        self.wxpusher_simple_url = "https://wxpusher.zjiecode.com/api/send/message/{}/{}"
        self.headers = {"Content-Type": "application/json"}
        self.proxies = {
            "http": os.getenv("http_proxy"),
            "https": os.getenv("https_proxy"),
        }

    def push_pushplus(self, content, token, is_success):
        attempts = 5
        title = f"微信阅读-{'成功' if is_success else '失败'}"
        for attempt in range(attempts):
            try:
                response = requests.post(
                    self.pushplus_url,
                    data=json.dumps({"token": token, "title": title,"content": content,}).encode("utf-8"),headers=self.headers,timeout=10,)
                response.raise_for_status()
                logger.info("PushPlus 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("PushPlus 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_telegram(self, content, bot_token, chat_id):
        url = self.telegram_url.format(bot_token)
        payload = {"chat_id": chat_id, "text": content}

        try:
            response = requests.post(url, json=payload, proxies=self.proxies, timeout=30)
            logger.info("Telegram 响应: %s", response.text)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram 代理发送失败: %s", exc)
            try:
                response = requests.post(url, json=payload, timeout=30)
                response.raise_for_status()
                return True
            except Exception as inner_exc:
                logger.error("Telegram 发送失败: %s", inner_exc)
                return False

    def push_wxpusher(self, content, spt):
        attempts = 5
        url = self.wxpusher_simple_url.format(spt, content)

        for attempt in range(attempts):
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                logger.info("WxPusher 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("WxPusher 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_serverChan(self, content, spt, is_success):
        attempts = 5
        url = self.server_chan_url.format(spt)

        title = f"微信阅读-{'成功' if is_success else '失败'}"

        for attempt in range(attempts):
            try:
                response = requests.post(
                    url,
                    data=json.dumps({"title": title, "desp": content}).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                response.raise_for_status()
                logger.info("ServerChan 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("ServerChan 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_email(self, content, is_success):
        try:
            if str(EMAIL_ENABLED).strip().lower() in {"0", "false", "no", "off"}:
                logger.info("邮件推送已禁用，跳过推送。")
                return False

            host = EMAIL_SMTP_HOST.strip()
            port = int(EMAIL_SMTP_PORT)
            sender = EMAIL_SENDER.strip()
            auth_code = EMAIL_AUTH_CODE
            recipients = [
                recipient.strip()
                for recipient in EMAIL_RECIPIENTS.replace(";", ",").split(",")
                if recipient.strip()
            ]
            if not host or not sender or not auth_code:
                raise ValueError(
                    "邮件配置不完整，需要设置 EMAIL_SMTP_HOST、EMAIL_SENDER、EMAIL_AUTH_CODE"
                )
            if not recipients:
                raise ValueError("需要设置 EMAIL_RECIPIENTS")

            title = f"微信阅读-{'成功' if is_success else '失败'}"
            message = EmailMessage()
            message["From"] = sender
            message["To"] = ", ".join(recipients)
            message["Subject"] = f"{EMAIL_SUBJECT_PREFIX}{title}"
            message.set_content(content)

            if port == 465:
                smtp = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                smtp = smtplib.SMTP(host, port, timeout=30)

            with smtp:
                smtp.ehlo()
                if port not in (25, 465):
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                smtp.login(sender, auth_code)
                smtp.send_message(message)

            logger.info("邮件推送成功，收件人数量：%d", len(recipients))
            return True
        except (KeyError, TypeError, ValueError, OSError, smtplib.SMTPException) as exc:
            logger.error("邮件推送失败: %s", exc)
            return False


def push(content, method, is_success = True):
    notifier = PushNotification()

    if method in (None, ""):
        logger.warning("未配置推送渠道，跳过推送。")
        return False

    method = str(method).lower()

    if method == "pushplus":
        return notifier.push_pushplus(content, PUSHPLUS_TOKEN, is_success)
    if method == "telegram":
        return notifier.push_telegram(content, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if method == "wxpusher":
        return notifier.push_wxpusher(content, WXPUSHER_SPT)
    if method == "serverchan":
        return notifier.push_serverChan(content, SERVERCHAN_SPT, is_success)
    if method == "email":
        return notifier.push_email(content, is_success)

    logger.warning("无效的通知渠道 '%s'，已跳过推送。支持：pushplus、telegram、wxpusher、serverchan、email", method)
    return False
