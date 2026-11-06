"""企业微信扫码/免密登录（自建中转服务版）。

链路：访问任意页面 → 未登录 302 到 /work-weixin/login/?next=<原路径>
  → 记录 next 到 cookie wework_next → 302 到企微授权页
  → 扫码 / 企微内免密授权 → 企微回跳统一入口
     http://login.tech.intra.nsfocus.com/{应用名}/work-weixin/oauth-callback
  → 网关按 {应用名} 原样转发到应用域名同路径 /work-weixin/oauth-callback
  → 校验 state → 调中转服务（code + state）换 userid → 建立会话
  → 302 回原路径，后续请求靠 session 保持登录。

本服务不持有 corpsecret / access_token；code 换 userid 全部由中转服务完成。
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

import requests
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect

logger = logging.getLogger(__name__)

# 固定接入信息（企微自建应用，由网关统一收口）
APPID = "wxa2938475051e2e83"
AGENTID = "1000188"
STATE = "auth_state"
AUTH_URL_BASE = "http://login.work.weixin.qq.com/wwlogin/sso/login"
# 统一回跳入口：{应用名} 即本服务在网关注册的名字（userai）
APP_NAME = os.environ.get("GITLABMR_WECOM_APP_NAME", "userai")
REDIRECT_URI = f"http://login.tech.intra.nsfocus.com/{APP_NAME}/work-weixin/oauth-callback"
# code 换 userid 的自建中转服务
WXLOGIN_SERVICE = "http://wxlogin.tech.intra.nsfocus.com/work-weixin/oauth-callback"
# 登录后目标页用 cookie 记录（state 被网关固定，无法携带自定义数据）
NEXT_COOKIE = "wework_next"
EXCHANGE_TIMEOUT = 10  # seconds


class WeWorkLoginError(Exception):
    """code 换 userid 失败（网络/非 200/响应异常）。"""


def build_auth_url() -> str:
    """企微授权页链接：redirect_uri、state 必须 URL 编码后拼接。"""
    query = urlencode(
        {
            "login_type": "CorpApp",
            "appid": APPID,
            "agentid": AGENTID,
            "redirect_uri": REDIRECT_URI,
            "state": STATE,
        }
    )
    return f"{AUTH_URL_BASE}?{query}"


def exchange_userid(code: str) -> str:
    """用 code + state 调中转服务换 userid（code 一次性、5 分钟有效，禁止缓存/重用）。"""
    try:
        resp = requests.get(
            WXLOGIN_SERVICE,
            params={"code": code, "state": STATE},
            timeout=EXCHANGE_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise WeWorkLoginError(f"请求中转服务失败: {exc}") from exc
    if resp.status_code != 200:
        raise WeWorkLoginError(f"中转服务返回 {resp.status_code}: {resp.text[:200]}")
    try:
        userid = str(resp.json().get("userid", "")).strip()
    except ValueError as exc:
        raise WeWorkLoginError(f"中转服务响应非 JSON: {resp.text[:200]}") from exc
    if not userid:
        raise WeWorkLoginError("中转服务未返回 userid")
    return userid


def wework_login(request):
    """登录入口：把回跳目标写进 cookie，再 302 到企微授权页。

    PC 浏览器打开即扫码页，企微内置浏览器自动免密授权，无需分支处理。
    """
    resp = redirect(build_auth_url())
    # 手册要求 httponly + samesite=Lax；Django 默认即 Lax，显式写清
    resp.set_cookie(NEXT_COOKIE, request.GET.get("next", "/"), max_age=300, httponly=True, samesite="Lax")
    return resp


def wework_oauth_callback(request):
    """企微回跳：校验 state → code 换 userid → 建立登录态 → 回原路径。

    回调是 GET，无需 CSRF 处理。
    """
    if request.GET.get("state") != STATE:
        return HttpResponse("invalid state", status=403)

    code = request.GET.get("code", "")
    if not code:
        return HttpResponse("missing code", status=400)
    try:
        userid = exchange_userid(code)
    except WeWorkLoginError as exc:
        logger.warning("企微登录失败: %s", exc)
        return JsonResponse({"error": "weixin login failed"}, status=502)

    request.session["ui_authed"] = True
    request.session["wework_userid"] = userid
    request.session.cycle_key()

    # 防开放重定向：只允许站内绝对路径
    next_url = request.COOKIES.get(NEXT_COOKIE, "/")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    resp = redirect(next_url)
    resp.delete_cookie(NEXT_COOKIE)
    return resp


def wework_logout(request):
    """登出：销毁会话后回到企微授权页（重新登录即重新授权）。"""
    request.session.flush()
    return redirect(build_auth_url())
