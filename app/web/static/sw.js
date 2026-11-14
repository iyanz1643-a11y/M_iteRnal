/* GA 업무 앱의 서비스 워커. 휴대폰 홈 화면 추가와 오프라인 안내를 맡는다.

   보관하는 것은 STATIC 목록의 정적 파일과 오프라인 안내뿐이다. 모두 /static/ 아래 주소다.
   로그인 화면, 로그인한 뒤의 화면, 자료는 보관하지 않는다. 개인정보와 실적이 기기에 남지 않게 하기 위해서다.
   - 화면 요청은 늘 서버로 보낸다. 연결이 없을 때만 오프라인 안내를 보여 준다.
   - 정적 파일은 서버의 새 것을 먼저 쓰고, 연결이 없을 때만 보관본을 쓴다.
   - 그 밖의 요청과 POST 는 손대지 않는다.
   tests/test_pwa.py 가 이 규칙을 코드로 검사한다. 목록을 바꾸면 CACHE 의 판 번호를 올린다. */
const CACHE = 'ga-static-v1';
const OFFLINE = '/static/offline.html';
const STATIC = [
  '/static/app.css',
  '/static/offline.html',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', function (event) {
  event.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(STATIC); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (event) {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(function () { return caches.match(OFFLINE); }));
    return;
  }
  if (STATIC.indexOf(url.pathname) < 0) return;
  event.respondWith(fetch(req).then(function (res) {
    if (res.ok && res.type === 'basic') {
      const copy = res.clone();
      caches.open(CACHE).then(function (c) { c.put(url.pathname, copy); });
    }
    return res;
  }).catch(function () { return caches.match(url.pathname); }));
});
