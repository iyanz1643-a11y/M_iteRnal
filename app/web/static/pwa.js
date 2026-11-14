/* 서비스 워커를 등록한다. 파일은 /static/ 에 있지만 앱 전체(/)의 오프라인 안내를 맡는다. app/web/main.py 의 AppStatic */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/static/sw.js', {scope: '/'}).catch(function () {});
  });
}
