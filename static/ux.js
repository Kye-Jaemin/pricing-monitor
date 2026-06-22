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
  //   선택한 탭은 sessionStorage 에 기억해, 소스 추가 등으로 페이지가 새로고침돼도
  //   같은 탭이 유지되도록 한다(전체로 튀지 않게).
  document.querySelectorAll('.cat-filter').forEach(function (catFilter) {
    var chips = catFilter.querySelectorAll('.cat-chip');
    var targetSel = catFilter.getAttribute('data-target') || '.cat-group';
    var targets = document.querySelectorAll(targetSel);
    var storeKey = 'catFilter:' + location.pathname + ':' + targetSel;
    // 숨겨진 대상의 폼 입력을 비활성화할지(예: 수집 대상 선택 — 안 보이면 제출 제외)
    var disableHidden = catFilter.hasAttribute('data-disable-hidden');

    function apply(sel) {
      var has = false;
      chips.forEach(function (c) { if (c.getAttribute('data-cat') === sel) has = true; });
      if (!has) sel = 'all';                 // 저장된 분류가 사라졌으면 전체로 폴백
      chips.forEach(function (c) {
        c.classList.toggle('on', c.getAttribute('data-cat') === sel);
      });
      targets.forEach(function (g) {
        var show = sel === 'all' || g.getAttribute('data-cat') === sel;
        g.style.display = show ? '' : 'none';
        if (disableHidden) {
          g.querySelectorAll('input, select, textarea, button').forEach(function (el) {
            el.disabled = !show;
          });
        }
      });
    }

    catFilter.addEventListener('click', function (e) {
      var btn = e.target.closest('.cat-chip');
      if (!btn) return;
      var sel = btn.getAttribute('data-cat');
      try { sessionStorage.setItem(storeKey, sel); } catch (_) {}
      apply(sel);
    });

    // 새로고침/리다이렉트 후 직전 선택 탭 복원
    var saved = null;
    try { saved = sessionStorage.getItem(storeKey); } catch (_) {}
    if (saved && saved !== 'all') apply(saved);
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
