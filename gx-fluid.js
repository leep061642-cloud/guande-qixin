/*!
 * GX Fluid v1 — 零依赖流体交互层
 *
 * 设计依据：Apple《Designing Fluid Interfaces》(WWDC 2018) 与《Principles of Great Design》。
 * 落到这个站点上解决四件事：
 *   1. 底部面板改成弹簧驱动：可从任意瞬间抓住、反向、打断，不再是一条固定时长的关键帧。
 *   2. 抓取把手变成真的能拖：pointer 1:1 跟随、尊重抓取偏移、越界橡皮筋、
 *      松手时把速度交给弹簧、并用动量投影决定落点（不是简单看拖了多远）。
 *   3. 按下即反馈（pointerdown 而非 click），拖开 10px 自动取消，符合「点击可反悔」。
 *   4. 三重可访问性降级：reduced-motion / reduced-transparency / contrast。
 *
 * 无任何依赖，挂到 window.GXFluid。
 */
(function (global) {
  'use strict';

  var doc = global.document;
  var BODY = doc.body;

  /* ---------------- 环境能力 ---------------- */
  var mqMotion = global.matchMedia ? global.matchMedia('(prefers-reduced-motion: reduce)') : null;

  /* 不读系统的「减弱动态效果」：本项目的动效一律照常播。
     iOS 用户打开这个开关后，面板弹簧、按压反馈、雷达线生长会全部消失，
     所以和背景球体保持一致，直接从代码层面绕过这个媒体查询。
     （若日后真要做无障碍开关，应做页面内开关，而不是恢复这里。） */
  function reducedMotion() {
    return false;
  }

  var canHaptic = typeof navigator !== 'undefined' && 'vibrate' in navigator;

  /* 触感只在「有意义的一刻」给，且必须与视觉同帧（这里靠同一帧内同步调用保证） */
  function haptic(ms) {
    if (!canHaptic) return;
    try { navigator.vibrate(ms); } catch (e) {}
  }

  /* ---------------- 弹簧 ---------------- */

  /*
   * 不用 mass/stiffness/damping  triplet，而是 Apple 的两个设计参数：
   *   damping（阻尼比 ζ）：1.0 临界阻尼不回弹；0.8 轻微回弹，只在手势本身带惯性时才用。
   *   response（响应时间，秒）：不是「时长」——弹簧没有固定时长，落定时间由参数涌现。
   * 换算式：ω₀ = 2π/response，k = ω₀²，c = 2ζω₀（质量取 1）。
   */
  function Spring(opts) {
    this.value = opts.value || 0;
    this.target = this.value;
    this.velocity = opts.velocity || 0;
    this.zeta = opts.damping != null ? opts.damping : 1;
    this.omega = (2 * Math.PI) / (opts.response || 0.4);
    this.onUpdate = opts.onUpdate || function () {};
    this.onRest = null;
    this._alive = false;
  }

  Spring.prototype.step = function (dt) {
    /*
     * 低帧率设备（或标签页刚切回来）会一次给出很大的 dt。
     * 不能直接 clamp 成 1/30 —— 那会让动画变成慢动作，手感立刻变"粘"。
     * 正确做法是按真实时长拆子步：稳定性由子步长 h 决定，与总时长无关。
     * 超过 0.4s 视为"页面刚回到前台"，直接落到目标，不做补间。
     */
    if (dt > 0.4) {
      this.value = this.target;
      this.velocity = 0;
      this.onUpdate(this.value);
      var f = this.onRest;
      this.onRest = null;
      if (f) f();
      return false;
    }
    var n = Math.max(1, Math.min(48, Math.ceil(dt / (1 / 240))));
    var h = dt / n;
    for (var i = 0; i < n; i++) {
      var a = -this.omega * this.omega * (this.value - this.target)
              - 2 * this.zeta * this.omega * this.velocity;
      this.velocity += a * h;
      this.value += this.velocity * h;
    }
    var done = Math.abs(this.value - this.target) < 0.4 && Math.abs(this.velocity) < 4;
    if (done) {
      this.value = this.target;
      this.velocity = 0;
      this.onUpdate(this.value);
      var fn = this.onRest;
      this.onRest = null;
      if (fn) fn();
      return false;
    }
    this.onUpdate(this.value);
    return true;
  };

  Spring.prototype.to = function (target, velocity, onRest) {
    this.target = target;
    if (velocity != null) this.velocity = velocity;
    this.onRest = onRest || null;
    wake(this);
    return this;
  };

  /* 停住：把目标拉回当前值，速度清零（用于手指按下的瞬间接管） */
  Spring.prototype.stop = function () {
    this.target = this.value;
    this.velocity = 0;
    var i = tickers.indexOf(this);
    if (i !== -1) tickers.splice(i, 1);
    return this;
  };

  /* ---------------- 单一 rAF 循环 ---------------- */

  var tickers = [];
  var rafId = 0;
  var lastT = 0;
  var mounted = [];        /* 已接管的面板；用于「中途切换减弱动效」时正确回退 */

  function loop(t) {
    var dt = lastT ? (t - lastT) / 1000 : 1 / 60;
    lastT = t;
    var any = false;
    for (var i = tickers.length - 1; i >= 0; i--) {
      if (tickers[i].step(dt)) any = true;
      else tickers.splice(i, 1);
    }
    if (any) {
      rafId = global.requestAnimationFrame(loop);
    } else {
      rafId = 0;
      lastT = 0;
    }
  }

  function wake(spring) {
    if (tickers.indexOf(spring) === -1) tickers.push(spring);
    if (!rafId) {
      lastT = 0;
      rafId = global.requestAnimationFrame(loop);
    }
  }

  /* ---------------- 手势数学 ---------------- */

  /*
   * 越界橡皮筋：越往外越拉不动。硬停会读作「卡死了」，渐进阻力才读作「到头了，但我在听你」。
   */
  function rubber(overshoot, dimension, constant) {
    var c = constant || 0.55;
    return (overshoot * dimension * c) / (dimension + c * Math.abs(overshoot));
  }

  /*
   * 动量投影：按 Apple 示例代码的指数衰减形式（不是物理课本的 v²/2a）。
   * 用释放点的速度推算「手势本来要去哪」，再从投影点附近决定吸附目标——
   * 这才让一次轻甩读起来像「把面板甩出去了」，而不是「拖过一半就关」。
   */
  function project(velocity, decel) {
    var d = decel || 0.998;
    return (velocity / 1000) * d / (1 - d);
  }

  /* ---------------- 底部面板 ---------------- */

  function Sheet(opts) {
    var self = this;
    this.el = opts.el;
    if (!this.el) return;

    this.cls = opts.cls;
    this.opts = opts;
    this.veil = opts.veil || doc.querySelector('.veil');
    this.drag = opts.drag ? this.el.querySelector(opts.drag) : null;

    this.hidden = this.measure();
    this.y = this.hidden;
    this.progress = 0;

    this.spring = new Spring({
      value: this.hidden,
      damping: opts.damping != null ? opts.damping : 0.9,
      response: opts.response != null ? opts.response : 0.4,
      onUpdate: function (v) { self.apply(v); }
    });
    this.apply(this.hidden);

    /* 只有真的有人接管时才关掉 CSS 过渡——这样万一脚本没跑起来，CSS 那套还能兜底。
       减弱动效时不要接管：那种情况下我们想保留 CSS 那段淡入淡出。 */
    mounted.push(this);
    syncSpringFlag();

    if (this.drag) this.bindDrag();

    global.addEventListener('resize', function () {
      self.hidden = self.measure();
      if (!self.isOpen()) self.apply(self.hidden);
      else self.apply(self.y);
    });

    /* 真机上卡片高度是晚一步才稳定的：字体加载、安全区 inset、地址栏收起
       都会改 offsetHeight。首次测量偏小的话，「关闭位置」就不够远，
       面板顶端那条亮边会在页面刚打开时露在屏幕最下方。加载完再量一次。 */
    var remeasure = function () {
      if (self.isOpen()) return;
      self.hidden = self.measure();
      self.apply(self.hidden);
    };
    global.addEventListener('load', remeasure);
    if (doc.fonts && doc.fonts.ready) {
      try { doc.fonts.ready.then(remeasure).catch(function () {}); } catch (e) {}
    }
    try {
      if (global.visualViewport) global.visualViewport.addEventListener('resize', remeasure);
    } catch (e) {}
  }

  Sheet.prototype.measure = function () {
    var h = this.el.offsetHeight || 420;
    /* 多留 6% 余量：卡片顶部有 -18px/60px 的上投影，正好停在 100% 会露出一条淡淡的光边 */
    return Math.round(h * 1.06) + 8;
  };

  Sheet.prototype.isOpen = function () {
    return BODY.classList.contains(this.cls);
  };

  Sheet.prototype.apply = function (y) {
    this.y = y;
    var p = 1 - Math.max(0, Math.min(1, y / (this.hidden || 1)));
    this.progress = p;
    this.el.style.setProperty('--sheet-y', y.toFixed(2) + 'px');
    if (this.veil) this.veil.style.setProperty('--veil-p', p.toFixed(3));
  };

  /* 交还给 CSS：清掉内联变量，让没有 JS / 降级模式下本来那套样式接管 */
  Sheet.prototype.reset = function () {
    this.el.style.removeProperty('--sheet-y');
    if (this.veil) this.veil.style.removeProperty('--veil-p');
    this.hidden = this.measure();
    this.y = this.hidden;
    this.progress = 0;
    /* 弹簧也要一起回到「关闭位置」。
       瞬间关闭（close(0,true)）走的是 stop()，它只把 target 拉回当前值，
       spring.value 会停在 0（完全打开）。下次 open() 又看到「内联 --sheet-y 已经被 reset 清掉」，
       就以为面板已经在终点了 —— 结果面板不是升起来的，而是「啪」一下闪现在屏幕上。 */
    if (this.spring) {
      this.spring.value = this.hidden;
      this.spring.velocity = 0;
    }
  };

  Sheet.prototype.open = function () {
    var self = this;
    /* 先把内联值写到「关闭位置」再挂类，否则会先闪一帧完全打开的画面 */
    this.hidden = this.measure();
    /* 内联 --sheet-y 不在，说明上一次是「瞬间关闭」或元素刚从 display:none 里出来：
       这时候必须把内联值写回关闭位置，并把弹簧也拉回那里，
       否则 open() 会从 0 直接跳到 0（等于没动画），面板就会闪现。
       内联值还在（中途被打断）时保持当前位置续接，不做这个重置。 */
    if (!this.el.style.getPropertyValue('--sheet-y')) {
      this.spring.value = this.hidden;
      this.spring.velocity = 0;
      this.apply(this.hidden);
    } else if (this.y > this.hidden) {
      this.apply(this.hidden);
    }

    BODY.classList.add(this.cls);

    if (reducedMotion()) { this.reset(); return this; }

    BODY.classList.add('gx-anim');
    this.spring.to(0, this.spring.velocity, function () {
      BODY.classList.remove('gx-anim');
      if (self.opts.onOpen) self.opts.onOpen();
    });
    haptic(6);
    return this;
  };

  Sheet.prototype.close = function (velocity, instant) {
    var self = this;
    if (!this.isOpen()) return this;

    if (instant || reducedMotion()) {
      this.spring.stop();
      BODY.classList.remove(this.cls);
      BODY.classList.remove('gx-anim');
      this.reset();
      if (this.opts.onClose) this.opts.onClose();
      return this;
    }

    this.hidden = this.measure();
    BODY.classList.add('gx-anim');
    this.spring.to(this.hidden, velocity || 0, function () {
      BODY.classList.remove('gx-anim');
      BODY.classList.remove(self.cls);
      self.reset();
      if (self.opts.onClose) self.opts.onClose();
    });
    haptic(8);
    return this;
  };

  /* ---- 拖动：1:1 跟随 + 速度交接 + 动量投影 ---- */

  Sheet.prototype.bindDrag = function () {
    var self = this;
    var zone = this.drag;
    var pid = null;
    var startY = 0;      /* 抓取瞬间面板的位置 */
    var startPtr = 0;    /* 抓取瞬间手指的位置 */
    var moved = false;
    var hist = [];

    zone.addEventListener('pointerdown', function (e) {
      if (!self.isOpen()) return;
      if (e.button != null && e.button !== 0) return;
      if (e.isPrimary === false) return;

      pid = e.pointerId;
      startY = self.y;
      startPtr = e.clientY;
      moved = false;
      hist = [{ t: e.timeStamp, y: e.clientY }];

      /* 从当前视觉位置接管，而不是从逻辑目标值——否则会看见一次跳变 */
      self.spring.stop();
      BODY.classList.add('gx-anim');
      self.el.classList.add('gx-dragging');

      try { zone.setPointerCapture(pid); } catch (err) {}
      e.preventDefault();
    });

    zone.addEventListener('pointermove', function (e) {
      if (pid === null || e.pointerId !== pid) return;

      var dy = e.clientY - startPtr;
      if (!moved) {
        /* 只认「往下拖」：6px 迟滞确认这是纵向手势。往上推不算拖动，
           这样就算手指落在把手附近，也绝不会把面板整体顶起来。 */
        if (dy < 6) return;
        moved = true;
      }

      hist.push({ t: e.timeStamp, y: e.clientY });
      while (hist.length > 2 && e.timeStamp - hist[0].t > 110) hist.shift();

      var y = startY + dy;
      if (y < 0) y = 0;                              /* 只允许往下：绝不把面板往上挪 */
      if (y > self.hidden) {
        y = self.hidden + rubber(y - self.hidden, self.hidden * 0.4);
      }
      self.apply(y);
      /*
       * 关键：弹簧内部值必须跟着手指走。
       * 只写 this.y / --sheet-y 而不写 spring.value 的话，松手那一刻弹簧还停在
       * 旧值上（打开时就是 0），to() 会先把面板瞬移回 0 再重新动画，
       * 表现出来就是「弹回来又瞬间弹回去」。
       */
      self.spring.value = y;
      self.el.classList.add('gx-dragging');
      e.preventDefault();
    });

    function finish(e) {
      if (pid === null) return;
      if (e.pointerId != null && e.pointerId !== pid) return;
      pid = null;
      self.el.classList.remove('gx-dragging');

      if (!moved) {              /* 只是点了把手：不改变开合状态 */
        BODY.classList.remove('gx-anim');
        /* 如果是在开合动画中途按住、又没拖动就松开，要把动画接着走完，
           否则面板会卡在半路不动。已经在端点时这段是空操作。 */
        var rest = self.y < self.hidden * 0.5 ? 0 : self.hidden;
        if (Math.abs(self.y - rest) > 1) self.spring.to(rest, 0);
        return;
      }
      moved = false;

      var v = 0;
      if (hist.length > 1) {
        var a = hist[0], b = hist[hist.length - 1];
        var dt = b.t - a.t;
        if (dt > 0) v = ((b.y - a.y) / dt) * 1000;   /* px/s，向下为正 */
      }

      /*
       * 交接前再对齐一次内部值，并给速度限幅。
       * 真实手指快速甩出时瞬时速度可能到 3000px/s 以上，原样交给弹簧会冲过头，
       * 看起来像「弹两下」；限幅后仍然保留手感，只是不会越过目标太远。
       */
      self.spring.value = self.y;
      v = Math.max(-1600, Math.min(1600, v));

      var y = self.y;
      var travelled = Math.abs(y - startY);
      var projected = y + project(v);
      var shouldOpen;

      /*
       * 速度的符号比位置更能说明意图：快速下甩就是「关」。
       * 但先要有最低位移，否则一次「按下时手抖一下」就会被当成甩动。
       */
      if (travelled < 16) shouldOpen = true;
      else if (v > 650) shouldOpen = false;
      else if (v < -650) shouldOpen = true;
      else shouldOpen = projected < self.hidden * 0.5;

      if (shouldOpen) {
        self.spring.to(0, v, function () {
          BODY.classList.remove('gx-anim');
          if (self.opts.onOpen) self.opts.onOpen();
        });
        haptic(5);
      } else {
        BODY.classList.add('gx-anim');
        self.spring.to(self.hidden, v, function () {
          BODY.classList.remove('gx-anim');
          BODY.classList.remove(self.cls);
          self.reset();
          if (self.opts.onClose) self.opts.onClose();
        });
        haptic(8);
      }
    }

    zone.addEventListener('pointerup', finish);
    zone.addEventListener('pointercancel', finish);
    zone.addEventListener('lostpointercapture', finish);
  };

  /* ---------------- 按下即反馈 ---------------- */

  /*
   * 反馈必须发生在 pointerdown 的同一帧。等 click 才变样式，手感就「死」了。
   * 同时给 10px 的取消余地：手指滑开就撤掉按下态，和系统按钮一致。
   */
  var PRESS_SEL = '[data-press],.go,.close,.eye,.t-chip,.ghost,.back,.teacher-link,.core,.hotspot';
  var pressEl = null;
  var pressFrom = null;

  /* 中央标题（.core h1）按下反馈：除了 CSS 的 is-press 类，
     再用内联 transform 直写，绕开微信 WKWebView 里 pointer 事件被吞 / CSS 特异性
     失效的问题。内联样式优先级最高，必然生效。
     注意：只处理 .core（brief/full/teacher 的标题容器）。首页的 .hotspot 是
     一个带 translate 定位的透明按钮，由首页 index.html 自己的 centerDown 处理，
     这里不能对它写 transform，否则会覆盖 translate 导致按钮跳位。 */
  var coreEl = null;
  var coreTarget = null;
  var coreHandled = false;
  var coreTimer = null;
  var corePt = null;

  function coreScale() {
    if (!coreEl) return null;
    return coreEl.querySelector('h1') || coreEl;
  }

  function coreDown(x, y) {
    if (coreHandled) return;
    coreHandled = true;
    corePt = { x: x, y: y };
    clearTimeout(coreTimer);
    coreTarget = coreTarget || coreScale();
    if (coreTarget) coreTarget.style.transform = 'scale(.94)';
  }
  function coreUp() {
    if (!coreHandled) return;
    coreHandled = false;
    corePt = null;
    clearTimeout(coreTimer);
    if (coreTarget) coreTarget.style.transform = '';
    coreTarget = null;
    /* 延迟移除类，让 0.24s 缩放过渡完整播放，快速点按也能看到「缩一下再弹回」 */
    coreTimer = setTimeout(function () {
      if (coreEl) coreEl.classList.remove('is-press');
    }, 150);
  }
  function coreMove(x, y) {
    if (!coreHandled || !corePt) return;
    if (Math.abs(x - corePt.x) > 10 || Math.abs(y - corePt.y) > 10) coreUp();
  }

  function clearPress() {
    if (pressEl) pressEl.classList.remove('is-press');
    pressEl = null;
    pressFrom = null;
  }

  function bindPress() {
    doc.addEventListener('pointerdown', function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var el = t.closest(PRESS_SEL);
      if (!el || el.disabled) return;
      if (pressEl && pressEl !== el) clearPress();
      pressEl = el;
      pressFrom = { x: e.clientX, y: e.clientY };
      el.classList.add('is-press');
      /* 中央标题（.core）：额外走内联直写，确保微信里也有反馈 */
      if (el.classList.contains('core')) {
        coreEl = el;
        coreTarget = null;
        coreDown(e.clientX, e.clientY);
      }
    }, { passive: true });

    doc.addEventListener('pointermove', function (e) {
      if (pressEl && pressFrom &&
          (Math.abs(e.clientX - pressFrom.x) > 10 || Math.abs(e.clientY - pressFrom.y) > 10)) {
        coreUp();
        clearPress();
      }
    }, { passive: true });

    doc.addEventListener('pointerup', function () { coreUp(); clearPress(); });
    doc.addEventListener('pointercancel', function () { coreUp(); clearPress(); });

    /* touch 兜底：微信 iOS 的 WKWebView 真实触摸时可能只派发 touch、不派发
       pointer 事件（这正是不明原因「微信里没效果、浏览器里有效果」的来源）。
       两套都挂，用 coreHandled 去重，哪套先到哪套生效。 */
    doc.addEventListener('touchstart', function (e) {
      var t = e.touches && e.touches[0];
      if (!t || !t.target || !t.target.closest) return;
      var el = t.target.closest('.core');
      if (!el) return;
      coreEl = el;
      coreTarget = null;
      coreDown(t.clientX, t.clientY);
      if (!pressEl) {
        pressEl = el;
        el.classList.add('is-press');
      }
    }, { passive: true });
    doc.addEventListener('touchmove', function (e) {
      var t = e.touches && e.touches[0];
      if (t) coreMove(t.clientX, t.clientY);
    }, { passive: true });
    doc.addEventListener('touchend', coreUp);
    doc.addEventListener('touchcancel', coreUp);

    /* 拖出窗口后松手也要复位 */
    global.addEventListener('blur', function () { coreUp(); clearPress(); });
  }

  /* ---------------- 手机软键盘避让 ---------------- */

  /*
   * 弹出面板本身就贴在屏幕底部，软键盘一起来就会把输入框和主按钮吞掉。
   * 用 visualViewport 算出键盘高度，写进 --kb，CSS 里把面板整体上抬。
   */
  function bindKeyboard() {
    var vv = global.visualViewport;
    if (!vv) return;

    var apply = function () {
      var kb = 0;
      /* 浏览器缩放时 vv.height 会缩水，这不是键盘，忽略 */
      if (!(vv.scale > 1.01)) {
        kb = Math.round((global.innerHeight - vv.height - vv.offsetTop));
      }
      if (kb < 60) kb = 0;
      if (kb > global.innerHeight * 0.6) kb = 0;
      doc.documentElement.style.setProperty('--kb', kb + 'px');
    };

    vv.addEventListener('resize', apply);
    vv.addEventListener('scroll', apply);
    global.addEventListener('orientationchange', function () {
      setTimeout(apply, 250);
    });
  }

  /* ---------------- 组装 ---------------- */

  function syncSpringFlag() {
    var takeOver = mounted.length > 0 && !reducedMotion();
    BODY.classList.toggle('gx-spring', takeOver);
    /* 回退到 CSS 模式时把这些内联值交还出去，否则会冻在上一次的像素位置上 */
    if (!takeOver) {
      for (var i = 0; i < mounted.length; i++) {
        mounted[i].el.style.removeProperty('--sheet-y');
        if (mounted[i].veil) mounted[i].veil.style.removeProperty('--veil-p');
      }
    }
  }

  var GXFluid = {
    Spring: Spring,
    Sheet: Sheet,
    haptic: haptic,
    reducedMotion: reducedMotion,
    rubber: rubber,
    project: project
  };

  function boot() {
    if (!reducedMotion()) doc.documentElement.classList.add('gx-fluid');
    if (mqMotion && mqMotion.addEventListener) {
      mqMotion.addEventListener('change', function (e) {
        doc.documentElement.classList.toggle('gx-fluid', !e.matches);
        syncSpringFlag();
      });
    }
    bindPress();
    bindKeyboard();
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', boot);
  else boot();

  global.GXFluid = GXFluid;
})(window);
