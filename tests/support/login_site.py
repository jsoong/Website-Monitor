"""A fixture "portal": login form, session cookie, protected pages, timeouts and challenges.

Everything a signed-in check needs to be tested against, on 127.0.0.1:

* ``/login``      a form (``#username``, ``#password``); a good POST sets the ``sid`` cookie and
                  redirects to ``/account``. ``challenge`` makes it ask for a one-time code
                  (``otp``) or a CAPTCHA instead of letting the login through.
* ``/account``    protected; its text is ``account_text`` (tests change it). Signed out, it
                  redirects to ``/login``.
* ``/enotices``   protected list page with links to ``/enotices/<n>.pdf`` (the ACCESS HRA
                  shape: the list may be read, a notice must never be opened). Every request is
                  logged in ``hits`` so a test can assert no PDF was fetched.
* ``/basic``, ``/digest``   HTTP Basic and Digest protected pages.
* ``/cookie-page``          200 only with the cookie ``token=<cookie_token>``, else 401.

Sessions end by ``expire_all()``, by an idle timeout (``idle_timeout_s``) or by an absolute one
(``absolute_timeout_s``), measured on ``now`` so a test can drive it from a fake clock.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qs

from aiohttp import web


def redirect(location: str) -> web.Response:
    return web.Response(status=302, headers={"Location": location})


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@dataclass
class LoginHit:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    status: int = 0


@dataclass
class _Session:
    created: float
    last_seen: float


@dataclass
class LoginSite:
    users: dict[str, str] = field(default_factory=lambda: {"alice": "s3cret-pw"})
    account_text: str = "Welcome Alice. Balance: 100"
    notices: list[str] = field(default_factory=lambda: ["Notice 1: appointment", "Notice 2: form"])
    challenge: Literal["otp", "captcha"] | None = None
    idle_timeout_s: float | None = None
    absolute_timeout_s: float | None = None
    cookie_token: str = "abc123"
    now: Callable[[], float] = time.monotonic
    hits: list[LoginHit] = field(default_factory=list)
    logins: list[str] = field(default_factory=list)  # user names of successful logins
    failed_logins: int = 0
    port: int = 0

    def __post_init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._runner: web.AppRunner | None = None
        self._nonce = secrets.token_hex(8)

    # -- test controls ------------------------------------------------------------------

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str = "/") -> str:
        return self.base + path

    def expire_all(self) -> None:
        self._sessions.clear()

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def hits_for(self, path: str) -> list[LoginHit]:
        return [h for h in self.hits if h.path == path]

    def pdf_hits(self) -> list[LoginHit]:
        return [h for h in self.hits if h.path.endswith(".pdf")]

    # -- sessions -----------------------------------------------------------------------

    def _session(self, request: web.Request) -> _Session | None:
        sid = request.cookies.get("sid")
        sess = self._sessions.get(sid or "")
        if sess is None:
            return None
        t = self.now()
        if self.idle_timeout_s is not None and t - sess.last_seen > self.idle_timeout_s:
            del self._sessions[sid or ""]
            return None
        if self.absolute_timeout_s is not None and t - sess.created > self.absolute_timeout_s:
            del self._sessions[sid or ""]
            return None
        sess.last_seen = t
        return sess

    # -- handlers -----------------------------------------------------------------------

    def _form(self, error: str = "", nxt: str = "/account") -> web.Response:
        extra = ""
        if self.challenge == "otp":
            extra = '<label>One-time code <input name="otp" id="otp"></label>'
        elif self.challenge == "captcha":
            extra = '<div class="g-recaptcha" id="captcha">Please verify you are human</div>'
        html = (
            "<html><head><title>Sign in</title></head><body><h1>Please sign in</h1>"
            f'<p class="error">{error}</p>'
            f'<form method="post" action="/login?next={nxt}">'
            '<label>User <input name="username" id="username"></label>'
            '<label>Password <input name="password" id="password" type="password"></label>'
            f'{extra}<button type="submit" id="go">Sign in</button></form></body></html>'
        )
        return web.Response(text=html, content_type="text/html")

    async def _login(self, request: web.Request) -> web.StreamResponse:
        nxt = request.query.get("next", "/account")
        if request.method == "GET":
            return self._form(nxt=nxt)
        data = parse_qs(await request.text())
        user = (data.get("username") or [""])[0]
        pw = (data.get("password") or [""])[0]
        if self.users.get(user) != pw or not pw:
            self.failed_logins += 1
            return self._form("Wrong user name or password", nxt)
        if self.challenge is not None:
            return self._form("Verification required", nxt)
        sid = secrets.token_hex(16)
        t = self.now()
        self._sessions[sid] = _Session(t, t)
        self.logins.append(user)
        resp = redirect(nxt if nxt.startswith("/") else "/account")
        resp.set_cookie("sid", sid, path="/", httponly=True)
        return resp

    def _protected(self, request: web.Request, body: str, title: str) -> web.StreamResponse:
        if self._session(request) is None:
            return redirect(f"/login?next={request.path}")
        return web.Response(
            text=f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>",
            content_type="text/html",
        )

    async def _account(self, request: web.Request) -> web.StreamResponse:
        return self._protected(request, f"<p>{self.account_text}</p>", "Account")

    async def _enotices(self, request: web.Request) -> web.StreamResponse:
        items = "".join(
            f'<li><a href="/enotices/{i}.pdf">{text}</a></li>'
            for i, text in enumerate(self.notices, 1)
        )
        return self._protected(request, f"<ul>{items}</ul>", "E-Notices")

    async def _notice_pdf(self, request: web.Request) -> web.StreamResponse:
        if self._session(request) is None:
            return redirect("/login")
        return web.Response(body=PDF, content_type="application/pdf")

    async def _logout(self, request: web.Request) -> web.StreamResponse:
        sid = request.cookies.get("sid")
        self._sessions.pop(sid or "", None)
        return redirect("/login")

    async def _basic(self, request: web.Request) -> web.StreamResponse:
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                user, pw = "", ""
            if self.users.get(user) == pw and pw:
                return web.Response(text=f"<html><body><p>{self.account_text}</p></body></html>",
                                    content_type="text/html")  # fmt: skip
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="fixture"'})

    async def _digest(self, request: web.Request) -> web.StreamResponse:
        header = request.headers.get("Authorization", "")
        challenge = (
            f'Digest realm="fixture", nonce="{self._nonce}", qop="auth", algorithm=MD5, opaque="op"'
        )
        if not header.startswith("Digest "):
            return web.Response(status=401, headers={"WWW-Authenticate": challenge})
        fields: dict[str, str] = {}
        for part in header[7:].split(","):
            key, _, value = part.strip().partition("=")
            fields[key] = value.strip('"')
        user = fields.get("username", "")
        pw = self.users.get(user)
        if pw is None or fields.get("nonce") != self._nonce:
            return web.Response(status=401, headers={"WWW-Authenticate": challenge})

        def md5(s: str) -> str:
            return hashlib.md5(s.encode()).hexdigest()

        ha1 = md5(f"{user}:fixture:{pw}")
        ha2 = md5(f"{request.method}:{fields.get('uri', request.path_qs)}")
        expected = md5(
            f"{ha1}:{self._nonce}:{fields.get('nc', '')}:{fields.get('cnonce', '')}:auth:{ha2}"
        )
        if fields.get("response") != expected:
            return web.Response(status=401, headers={"WWW-Authenticate": challenge})
        return web.Response(text=f"<html><body><p>{self.account_text}</p></body></html>",
                            content_type="text/html")  # fmt: skip

    async def _cookie_page(self, request: web.Request) -> web.StreamResponse:
        if request.cookies.get("token") != self.cookie_token:
            return web.Response(status=401, text="cookie required")
        return web.Response(text=f"<html><body><p>{self.account_text}</p></body></html>",
                            content_type="text/html")  # fmt: skip

    @web.middleware
    async def _log(
        self, request: web.Request, handler: Callable[[web.Request], web.StreamResponse]
    ) -> web.StreamResponse:
        hit = LoginHit(request.method, request.path, request.query_string, dict(request.headers))
        self.hits.append(hit)
        resp = await handler(request)
        hit.status = resp.status
        return resp

    # -- server -------------------------------------------------------------------------

    async def start(self) -> LoginSite:
        app = web.Application(middlewares=[self._log])
        app.router.add_route("*", "/login", self._login)
        app.router.add_get("/account", self._account)
        app.router.add_get("/enotices", self._enotices)
        app.router.add_get(r"/enotices/{name}.pdf", self._notice_pdf)
        app.router.add_get("/logout", self._logout)
        app.router.add_get("/basic", self._basic)
        app.router.add_get("/digest", self._digest)
        app.router.add_get("/cookie-page", self._cookie_page)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        assert self._runner.addresses
        self.port = self._runner.addresses[0][1]
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
