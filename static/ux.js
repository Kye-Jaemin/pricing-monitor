/* 화려한 UX 보조 스크립트 — 스크롤 진행바·등장 애니메이션·카드 스포트라이트·버튼 리플.
   prefers-reduced-motion 을 존중하고, JS 미동작/비활성 시에도 콘텐츠는 그대로 보인다. */
(function () {
  var reduce = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.body.classList.add('js-ux');

  // 1) 상단 스크롤 진행바
  var sp = document.getElementById('scrollProgress');
  function onScroll() {
    var h = document.documentElement;
    var max = h.scrollHeight - h.clientHeight;
    if (sp) sp.style.width = (max > 0 ? (h.scrollTop / max) * 100 : 0) + '%';
  }
  document.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // 2) 스크롤 등장(reveal)
  var items = document.querySelectorAll('.card, .saved-card, .empty');
  if (reduce || !('IntersectionObserver' in window)) {
    items.forEach(function (el) { el.classList.add('in'); });
  } else {
    items.forEach(function (el) { el.classList.add('reveal'); });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { threshold: 0.06, rootMargin: '0px 0px -40px 0px' });
    items.forEach(function (el) { io.observe(el); });
  }

  // 2.5) 업체 분류 필터(칩 클릭 시 해당 분류만 표시).
  //   대상은 data-target 선택자(현황=.cat-group, 업체관리=.company-admin-card).
  document.querySelectorAll('.cat-filter').forEach(function (catFilter) {
    var chips = catFilter.querySelectorAll('.cat-chip');
    var targetSel = catFilter.getAttribute('data-target') || '.cat-group';
    var targets = document.querySelectorAll(targetSel);
    catFilter.addEventListener('click', function (e) {
      var btn = e.target.closest('.cat-chip');
      if (!btn) return;
      var sel = btn.getAttribute('data-cat');
      chips.forEach(function (c) { c.classList.toggle('on', c === btn); });
      targets.forEach(function (g) {
        var show = sel === 'all' || g.getAttribute('data-cat') === sel;
        g.style.display = show ? '' : 'none';
      });
    });
  });

  if (reduce) return;

  // 3) 카드 커서 추적 스포트라이트
  document.addEventListener('mousemove', function (e) {
    var c = e.target.closest && e.target.closest('.card');
    if (!c) return;
    var r = c.getBoundingClientRect();
    c.style.setProperty('--mx', ((e.clientX - r.left) / r.width * 100) + '%');
    c.style.setProperty('--my', ((e.clientY - r.top) / r.height * 100) + '%');
  }, { passive: true });

  // 4) 버튼 클릭 리플
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.btn-primary, .btn-secondary');
    if (!b) return;
    var r = b.getBoundingClientRect();
    var size = Math.max(r.width, r.height);
    var s = document.createElement('span');
    s.className = 'ripple';
    s.style.width = s.style.height = size + 'px';
    s.style.left = (e.clientX - r.left - size / 2) + 'px';
    s.style.top = (e.clientY - r.top - size / 2) + 'px';
    b.appendChild(s);
    setTimeout(function () { if (s.parentNode) s.parentNode.removeChild(s); }, 650);
  });
})();
