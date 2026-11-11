(function () {
  'use strict';
  var titles = { reports: '报告（规划中）', monitor: '监控（规划中）', ops: '运维（规划中）' };
  function show(route) {
    var views = document.querySelectorAll('.view');
    for (var i = 0; i < views.length; i++) views[i].hidden = true;
    var id = route === 'qd' ? 'view-qd' : (route === 'home' ? 'view-home' : 'view-placeholder');
    document.getElementById(id).hidden = false;
    if (route === 'qd') {
      var f = document.getElementById('qd-frame');
      if (!f.getAttribute('src')) f.setAttribute('src', f.getAttribute('data-src'));
    }
    if (id === 'view-placeholder') document.getElementById('ph-title').textContent = titles[route] || '规划中';
    var links = document.querySelectorAll('#nav a[data-route]');
    for (var j = 0; j < links.length; j++) links[j].classList.toggle('active', links[j].getAttribute('data-route') === route);
  }
  function route() {
    var m = (location.hash || '#/home').match(/^#\/([a-z]+)/);
    show(m ? m[1] : 'home');
  }
  window.addEventListener('hashchange', route);
  route();
})();
