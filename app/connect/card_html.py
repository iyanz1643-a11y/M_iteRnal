"""명함 신청본 HTML 을 앱 화면의 sandbox iframe 에 넣을 수 있게 다듬는다.

신청본 index.html 은 남이 만든 HTML 이다. 세 겹으로 막는다.
1. 다듬기: 아래 clean_card_html 이 HTML 을 한 번 읽고 허용한 것만 다시 쓴다.
   - script, iframe, object, embed, template, foreignObject 같은 것은 내용째 버린다.
   - base, link, meta, form 은 태그만 버린다. 주석과 처리 지시도 버린다.
   - on 으로 시작하는 속성, srcdoc, action, formaction, http-equiv, ping, target 을 버린다.
   - a 와 area 의 href 는 지운다. 미리보기 안에서 다른 곳으로 가지 않는다.
     심사하는 사람이 링크 대상을 볼 수 있게 http, https, tel, mailto, sms 주소는 title 로 옮긴다.
   - 그림 주소는 신청본 안의 JPEG(og.jpg, card.jpg)만 남긴다. inline 으로 받은 바이트는 data: 주소로 넣고,
     없으면 화면이 준 주소 틀로 바꾼다. 둘 다 없거나 바깥 주소면 지운다.
   - CSS 의 url() 도 같은 규칙으로 바꾸고, @import, expression(, javascript: 는 지운다.
   - 맨 앞에 Content-Security-Policy meta 를 넣어 스크립트와 바깥 요청을 막는다.
2. iframe: 화면은 sandbox 속성을 값 없이 둔다. 스크립트, 같은 출처 권한, 폼, 팝업, 위 창 이동이 모두 꺼진다.
3. 응답 머리글: 주소로 내보낼 때는 preview_headers 를 붙인다. CSP sandbox 로 주소를 바로 열어도 같은 제한이 걸린다.

앱의 세션 쿠키는 SameSite=Lax 다. sandbox iframe 안의 문서는 출처가 없어 그 안에서 앱 주소로 그림을 부르면 쿠키가 붙지 않는다.
그래서 기본은 그림을 data: 로 넣는 inline 이고, 주소 틀은 화면이 쿠키 없이 열리는 주소를 따로 만들 때만 쓴다.
"""
from __future__ import annotations

import base64
import html
import posixpath
import re
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import quote, unquote, urlsplit

from app.connect.card_site import FILE_TYPES, valid_file_path

IFRAME_SANDBOX = ""                                   # sandbox 속성 값. 비워 두면 모든 제한이 걸린다
IFRAME_ATTRS = {"sandbox": IFRAME_SANDBOX, "referrerpolicy": "no-referrer", "loading": "lazy"}

# 내용째 버리는 태그
_DROP_ALL = frozenset({"script", "iframe", "frame", "frameset", "object", "applet", "embed", "noembed", "noframes",
                       "template", "foreignobject", "portal", "xmp", "plaintext"})
# 태그만 버리고 안의 글은 남긴다. 빈 태그도 여기 둔다
_DROP_TAG = frozenset({"base", "link", "meta", "form", "param", "animate", "set", "animatemotion", "animatetransform",
                       "handler", "listener", "noscript"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_DROP_ATTR = frozenset({"srcdoc", "action", "formaction", "http-equiv", "ping", "target", "nonce", "integrity", "manifest",
                        "codebase", "data", "dynsrc", "lowsrc", "attributename", "from", "to", "values", "by"})
_URL_ATTR = frozenset({"src", "href", "xlink:href", "poster", "background"})
_LINK_TAGS = frozenset({"a", "area"})
_NAME_RE = re.compile(r"[a-z][a-z0-9-]*(:[a-z][a-z0-9-]*)?")
_ATTR_RE = re.compile(r"[a-z_:][a-z0-9_:.-]*")
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
_CSS_BAD = re.compile(r"@import[^;]*;?|expression\s*\(|javascript\s*:|vbscript\s*:|-moz-binding|behavior\s*:", re.I)
_CTRL = re.compile(r"[\x00-\x20\x7f]+")
TITLE_MAX = 200
_SHOW_SCHEMES = frozenset({"", "http", "https", "tel", "mailto", "sms"})   # 링크 주소를 title 로 보여 주는 종류


def csp(asset_src: str = "") -> str:
    """미리보기 문서의 CSP. 그림은 data: 와 화면이 준 출처만, 스크립트와 바깥 요청은 없다."""
    img = "data:" + (f" {asset_src}" if asset_src else "")
    return (f"default-src 'none'; img-src {img}; style-src 'unsafe-inline'; font-src data:; media-src data:; "
            "base-uri 'none'; form-action 'none'")


def preview_headers(content_type: str, asset_src: str = "") -> dict:
    """화면이 미리보기를 주소로 내보낼 때 붙이는 응답 머리글. HTML 이면 CSP sandbox 를 건다."""
    h = {"content-type": content_type, "x-content-type-options": "nosniff", "cache-control": "no-store",
         "referrer-policy": "no-referrer", "cross-origin-resource-policy": "same-origin"}
    if content_type.startswith("text/html"):
        h["content-security-policy"] = f"sandbox; {csp(asset_src)}; frame-ancestors 'self'"
    else:
        h["content-security-policy"] = "default-src 'none'; sandbox"
        h["content-disposition"] = "inline"
    return h


def _asset_src(asset_url: str | None) -> str:
    if not asset_url:
        return ""
    s = urlsplit(asset_url)
    if s.scheme in ("http", "https") and s.netloc:
        return f"{s.scheme}://{s.netloc}"
    return "'self'"


# ------------------------------------------------------------------ 주소 풀기
class _Resolver:
    def __init__(self, slug: str, page_path: str, site_base: str | None):
        self.slug = slug
        self.base_dir = posixpath.dirname(page_path)
        b = urlsplit(site_base or "")
        self.site = (b.scheme.lower(), b.netloc.lower()) if b.scheme in ("http", "https") and b.netloc else None

    def asset(self, ref) -> str | None:
        """참조 하나를 신청본 안 JPEG 경로로. 아니면 None."""
        ref = _CTRL.sub("", html.unescape(str(ref or "")))
        if not ref or ref.startswith("#"):
            return None
        s = urlsplit(ref)
        prefix = f"/{self.slug}/"
        if s.scheme or s.netloc:
            if not self.site or (s.scheme.lower(), s.netloc.lower()) != self.site:
                return None
            if not s.path.startswith(prefix):
                return None
            rel = s.path[len(prefix):]
        elif s.path.startswith("/"):
            if not s.path.startswith(prefix):
                return None
            rel = s.path[len(prefix):]
        else:
            rel = posixpath.join(self.base_dir, s.path)
        rel = posixpath.normpath(unquote(rel))
        if rel.startswith("../") or rel in ("..", ".") or rel.startswith("/"):
            return None
        if valid_file_path(rel) and rel.endswith(".jpg"):
            return rel
        return None


# ------------------------------------------------------------------ 다듬기
class _Cleaner(HTMLParser):
    def __init__(self, resolver: _Resolver, rewrite, meta: str | None):
        super().__init__(convert_charrefs=True)
        self.r = resolver
        self.rewrite = rewrite                  # 신청본 경로 -> 바꿀 주소. None 이면 지운다
        self.meta = meta
        self.out: list = []
        self.skip: list = []                    # 내용째 버리는 중인 태그
        self.found: list = []                   # 찾은 그림 경로
        self._head_done = False

    # 결과
    def _emit(self, text: str) -> None:
        if not self._head_done:
            self._head_done = True
            if self.meta:
                self.out.append(self.meta)
        self.out.append(text)

    def _url(self, ref) -> str | None:
        path = self.r.asset(ref)
        if path is None:
            return None
        if path not in self.found:
            self.found.append(path)
        return self.rewrite(path)

    def _css(self, text: str) -> str:
        def sub(m):
            new = self._url(m.group(2))
            return f'url("{new}")' if new else "none"
        return _CSS_BAD.sub("", _CSS_URL.sub(sub, text))

    def _srcset(self, value: str) -> str | None:
        keep = []
        for part in value.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            new = self._url(bits[0])
            if new:
                keep.append(" ".join([new, *bits[1:2]]))
        return ", ".join(keep) or None

    def _attrs(self, tag: str, attrs) -> str:
        out, title = [], None
        has_title = any(k == "title" for k, _ in attrs)
        for k, v in attrs:
            k = (k or "").lower()
            if not _ATTR_RE.fullmatch(k) or k.startswith("on") or k in _DROP_ATTR:
                continue
            if v is None:
                out.append(k)
                continue
            if k == "style":
                v = self._css(v)
            elif k == "srcset" or k == "imagesrcset":
                v = self._srcset(v)
            elif k in _URL_ATTR:
                if tag in _LINK_TAGS and k in ("href", "xlink:href"):
                    shown = _CTRL.sub(" ", v).strip()
                    if not has_title and title is None and urlsplit(_CTRL.sub("", v)).scheme.lower() in _SHOW_SCHEMES:
                        title = shown[:TITLE_MAX] or None
                    continue
                if v.strip().startswith("#") and k in ("href", "xlink:href"):
                    pass                        # 문서 안 참조. svg use 같은 것
                else:
                    v = self._url(v)
            if v is None:
                continue
            out.append(f'{k}="{html.escape(v, quote=True)}"')
        if title:
            out.append(f'title="{html.escape(title, quote=True)}"')
        return (" " + " ".join(out)) if out else ""

    # 파서
    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag, attrs, closed):
        tag = (tag or "").lower()
        if self.skip:
            if tag == self.skip[-1] and not closed and tag not in _VOID:
                self.skip.append(tag)
            return
        if tag in _DROP_ALL:
            if not closed and tag not in _VOID:
                self.skip.append(tag)
            return
        if tag in _DROP_TAG or not _NAME_RE.fullmatch(tag):
            return
        self._emit(f"<{tag}{self._attrs(tag, attrs)}{' /' if closed else ''}>")

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in _DROP_ALL or tag in _DROP_TAG or tag in _VOID or not _NAME_RE.fullmatch(tag):
            return
        self._emit(f"</{tag}>")

    def handle_data(self, data):
        if self.skip:
            return
        if self.cdata_elem == "style":
            self._emit(self._css(data).replace("</", "<\\/"))
        else:
            self._emit(html.escape(data, quote=False))

    def handle_decl(self, decl):
        if not self.skip and not self.out and decl.lower().startswith("doctype"):
            self.out.append("<!DOCTYPE html>")

    def handle_comment(self, data):
        return

    def handle_pi(self, data):
        return

    def unknown_decl(self, data):
        return

    def finish(self) -> str:
        self.close()
        if not self._head_done:
            self._emit("")
        return "".join(self.out)


def _decode(raw) -> str:
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    return text.lstrip("\ufeff").lstrip()


def referenced_assets(raw, *, slug: str, page_path: str = "index.html", site_base: str | None = None) -> list:
    """신청본 HTML 이 부르는 신청본 안 JPEG 경로. 미리보기에 넣을 그림을 미리 가져올 때 쓴다."""
    c = _Cleaner(_Resolver(slug, page_path, site_base), rewrite=lambda p: None, meta=None)
    c.feed(_decode(raw))
    c.finish()
    return list(c.found)


def asset_address(template: str, slug: str, path: str) -> str:
    """주소 틀의 {slug} 와 {path} 를 채운다. format 을 쓰지 않아 틀의 다른 중괄호를 건드리지 않는다."""
    return template.replace("{slug}", quote(slug, safe="")).replace("{path}", quote(path, safe="/"))


def clean_card_html(raw, *, slug: str, page_path: str = "index.html", asset_url: str | None = None,
                    inline: dict | None = None, site_base: str | None = None) -> str:
    """신청본 HTML 을 sandbox iframe 용으로 다듬어 글로 돌려준다.

    - inline: {신청본 경로: JPEG 바이트}. 있으면 그 그림을 data: 주소로 넣는다.
    - asset_url: 화면의 그림 주소 틀. 예: "/cards/preview/{slug}/{path}". inline 에 없는 그림에 쓴다.
    - site_base: 명함 사이트 주소. 신청본이 자기 공개 주소로 그림을 부르면 신청본 경로로 바꿔 읽는다.
    둘 다 없으면 그림 주소를 지운다. 바깥 주소는 늘 지운다."""
    inline = inline or {}

    def rewrite(path: str) -> str | None:
        data = inline.get(path)
        if data:
            return f"data:{FILE_TYPES['jpg']};base64,{base64.b64encode(bytes(data)).decode('ascii')}"
        if asset_url:
            return asset_address(asset_url, slug, path)
        return None

    meta = (f'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" '
            f'content="{html.escape(csp(_asset_src(asset_url)), quote=True)}">'
            '<meta name="referrer" content="no-referrer">')
    c = _Cleaner(_Resolver(slug, page_path, site_base), rewrite=rewrite, meta=meta)
    c.feed(_decode(raw))
    return c.finish()


def iframe_tag(src: str | None = None, *, srcdoc: str | None = None, title: str = "명함 미리보기",
               extra: Iterable = ()) -> str:
    """화면이 그대로 쓸 수 있는 iframe 한 줄. src 나 srcdoc 중 하나를 준다. 값은 모두 따옴표 안으로 이스케이프한다."""
    attrs = [("sandbox", IFRAME_SANDBOX), ("referrerpolicy", "no-referrer"), ("loading", "lazy"), ("title", title), *extra]
    if srcdoc is not None:
        attrs.append(("srcdoc", srcdoc))
    elif src is not None:
        attrs.append(("src", src))
    return "<iframe " + " ".join(f'{k}="{html.escape(str(v), quote=True)}"' for k, v in attrs) + "></iframe>"
