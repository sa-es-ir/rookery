/* The run as an animated sequence diagram. The landing page, the docs and the portal's Workflow
   view all draw it with Flow.draw(box, spec): three lanes (you · agents · rookery), time runs down,
   one row per node unless it says `same`. A packet walks spec.run edge by edge and the panel under
   the diagram narrates it; clicking a node stops the walk and shows that node's card.
   Every string goes in as text, never markup: the portal feeds it values read from a target repo. */
const Flow = (() => {
  const NS = 'http://www.w3.org/2000/svg', KIND = ['you', 'agent', 'bot'], LANES = ['You', 'Agents', 'Rookery'];
  const GAP = 56, HEAD = 34;   // the band between rows holds the wire labels; HEAD the lane names
  const still = matchMedia('(prefers-reduced-motion: reduce)');
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let uid = 0;

  // `x` in backticks becomes <code>; the rest stays text
  function text(e, s){
    for (const p of String(s).split(/(`[^`]+`)/))
      if (p) e.append(/^`[^`]+`$/.test(p) ? Object.assign(document.createElement('code'), {textContent: p.slice(1, -1)}) : p);
    return e;
  }
  function el(tag, cls, s){
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (s != null) text(e, s);
    return e;
  }
  function svg(tag, attrs){
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  // a node's card in the .flow .node styles (styles.css): the panel's view of a node with no detail()
  function card(n){
    const c = el('div', 'node ' + (n.lane === 1 ? '' : KIND[n.lane])), head = el('header');
    head.append(el('span', 'kind', n.lane === 2 ? 'rookery' : KIND[n.lane]), el('h3', null, n.title));
    if (n.label) head.append(el('span', 'lbl ' + (n.tone || ''), n.label));
    c.append(head);
    if (n.who){
      const w = el('div', 'who');
      w.append(el('b', null, n.who), n.model ? ' · ' + n.model : '');
      c.append(w);
    }
    if (n.note) c.append(el('p', 'note', n.note));
    if (n.again) c.append(el('p', 'again', n.again));
    return c;
  }

  function draw(box, spec){
    const id = ++uid, stage = el('div', 'fx-stage'), wires = svg('svg', {class: 'fx-wires', 'aria-hidden': 'true'});
    const grad = svg('linearGradient', {id: 'fx-g' + id, gradientUnits: 'userSpaceOnUse', x1: 0, y1: 0});
    grad.append(svg('stop', {offset: 0, 'stop-color': '#077ac7'}), svg('stop', {offset: 1, 'stop-color': '#6b21ef'}));
    const mark = svg('marker', {id: 'fx-m' + id, viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 8, markerHeight: 8,
      markerUnits: 'userSpaceOnUse', orient: 'auto'});
    mark.append(svg('path', {d: 'M0 0L8 4L0 8z', fill: '#6b21ef'}));
    const defs = svg('defs'), lines = svg('g'), dot = svg('circle', {class: 'fx-dot', r: 4.5, style: 'display:none'});
    defs.append(grad, mark);
    wires.append(defs, lines, dot);
    stage.append(wires, ...LANES.map((t, i) => el('span', 'fx-lane ' + KIND[i], t)));

    const N = new Map();
    for (const n of spec.nodes){
      const b = el('button', 'fx-node ' + KIND[n.lane] + (n.tag ? ' opt' : ''));
      b.type = 'button';
      b.append(el('b', null, n.title));
      if (n.who){
        const w = el('span', 'fx-who', n.who);
        if (n.model) w.append(el('span', 'fx-model', ' · ' + n.model));   // phones drop it; the card has it
        b.append(w);
      }
      if (n.tag) b.append(el('span', 'fx-tag', n.tag));
      if (n.loop) b.append(el('span', 'fx-loop', n.loop));
      b.onclick = () => { stop(); show(n.id, '', true); };
      stage.append(b);
      N.set(n.id, {n, b});
    }

    const say = el('p', 'fx-say'), ctl = el('button', 'frost fx-ctl'), slot = el('div');
    ctl.type = 'button';
    ctl.onclick = () => running ? stop() : play();
    const bar = el('div', 'fx-bar');
    bar.append(say, ctl);
    const panel = el('div', 'fx-panel flow');
    panel.append(bar, slot);
    box.classList.add('fx');
    box.replaceChildren(stage, panel);

    /* --- layout: re-run whenever the box changes width, since node heights follow it --- */
    const P = new Map();   // 'from>to' → its <path>, for the packet
    let W = 0;
    function layout(){
      const w = stage.clientWidth;
      if (!w || w === W) return;   // hidden (the portal's views), or nothing changed
      W = w;
      const lw = w / 3, nw = Math.min(240, lw - Math.max(16, lw * .14)), gut = (lw - nw) / 2;
      for (const {n, b} of N.values()){
        b.style.width = nw + 'px';
        b.style.left = n.lane * lw + gut + 'px';
      }
      let top = HEAD, h = 0;
      for (const {n, b} of N.values()){
        if (!n.same && h){ top += h + GAP; h = 0; }
        b.style.top = top + 'px';
        h = Math.max(h, b.offsetHeight);
      }
      const H = top + h + 4;
      stage.style.height = H + 'px';
      stage.querySelectorAll('.fx-lane').forEach((s, i) => s.style.left = lw * i + lw / 2 + 'px');
      grad.setAttribute('x2', w); grad.setAttribute('y2', H);

      lines.replaceChildren();
      stage.querySelectorAll('.fx-cond').forEach(s => s.remove());
      for (let i = 0; i < 3; i++)
        lines.append(svg('line', {class: 'fx-life', x1: lw * i + lw / 2, x2: lw * i + lw / 2, y1: HEAD - 6, y2: H}));
      const R = k => { const b = N.get(k).b; return {x: b.offsetLeft, y: b.offsetTop, w: b.offsetWidth, h: b.offsetHeight}; };
      for (const [from, to, label, cls] of spec.edges){
        const a = R(from), b = R(to);
        let d, up = false;
        if (from === to){   // a node that repeats: an arc in the gutter on its right
          const x = a.x + a.w, c = x + gut * 1.8;
          d = `M${x} ${a.y + a.h * .3}C${c} ${a.y + a.h * .3} ${c} ${a.y + a.h * .7} ${x} ${a.y + a.h * .7}`;
        } else if (Math.abs(a.y - b.y) < 2){   // same row: two straight wires, one each way
          const y = a.y + Math.min(a.h, b.h) / 2 + (b.x > a.x ? 7 : -7);
          d = b.x > a.x ? `M${a.x + a.w} ${y}H${b.x}` : `M${a.x} ${y}H${b.x + b.w}`;
        } else if (b.y > a.y){   // down to a later row: bottom to top
          const x1 = a.x + a.w / 2, y1 = a.y + a.h, x2 = b.x + b.w / 2, m = (y1 + b.y) / 2;
          d = `M${x1} ${y1}C${x1} ${m} ${x2} ${m} ${x2} ${b.y}`;
        } else {   // back up to an earlier row: through the left gutter, which no node uses
          const g = Math.min(a.x, b.x) - gut * .6;
          d = `M${a.x} ${a.y + a.h / 2}H${g}V${b.y + b.h / 2}H${b.x}`;
          up = true;
        }
        const p = svg('path', {d, class: 'fx-wire ' + (cls || ''), stroke: `url(#fx-g${id})`, 'marker-end': `url(#fx-m${id})`});
        lines.append(p);
        P.set(from + '>' + to, p);
        if (!label || from === to || Math.abs(a.y - b.y) < 2) continue;   // no room for one there
        const at = p.getPointAtLength(p.getTotalLength() / 2), s = el('span', 'fx-cond' + (up ? ' up' : ''), label);
        s.style.left = at.x + 'px'; s.style.top = at.y + 'px';
        stage.append(s);
      }
    }
    new ResizeObserver(layout).observe(stage);

    /* --- the walk --- */
    let gen = 0, running = false;
    function show(k, line, pinned){
      const {n} = N.get(k);
      for (const o of N.values()) o.b.classList.toggle('on', o.n.id === k);
      say.textContent = line;
      slot.replaceChildren(pinned && n.detail ? n.detail() : card(n));
    }
    function stop(){ gen++; running = false; ctl.textContent = '▶ Play'; dot.style.display = 'none'; }
    function move(k, g){
      const p = P.get(k);
      if (still.matches || !p) return Promise.resolve();
      const len = p.getTotalLength(), dur = Math.min(1300, 450 + len * 2), t0 = performance.now();
      p.classList.add('hot');
      dot.style.display = '';
      return new Promise(done => {
        const f = now => {
          const t = Math.min(1, (now - t0) / dur), off = g !== gen || !box.isConnected;
          if (!off){
            const at = p.getPointAtLength(len * (t < .5 ? 2 * t * t : 1 - (2 - 2 * t) ** 2 / 2));
            dot.setAttribute('cx', at.x); dot.setAttribute('cy', at.y);
          }
          if (t < 1 && !off) return requestAnimationFrame(f);
          p.classList.remove('hot');
          dot.style.display = 'none';
          done();
        };
        requestAnimationFrame(f);
      });
    }
    async function play(){
      const g = ++gen, live = () => g === gen && box.isConnected;
      running = true;
      ctl.textContent = '❚❚ Pause';
      while (live()){
        for (const o of N.values()) o.b.classList.remove('seen');
        show(spec.run[0][0], spec.intro);
        await sleep(1800);
        for (const [from, to, line] of spec.run){
          if (!live()) return;
          await move(from + '>' + to, g);
          if (!live()) return;
          N.get(from).b.classList.add('seen');
          show(to, line);
          await sleep(still.matches ? 3200 : 2200);
        }
        await sleep(2400);
      }
    }

    // ponytail: starts once, the first time half of it is on screen; it then runs until paused
    stop();
    show(spec.run[0][0], spec.intro);
    if (!still.matches) new IntersectionObserver((es, io) => {
      if (es.some(e => e.isIntersecting)){ io.disconnect(); if (!gen || gen === 1) play(); }
    }, {threshold: .5}).observe(stage);
  }

  /* --- the two flows. issue() and pr() are what the portal fills with a repo's real config;
     LOOP and PR below are the same flows with the product's agent roster. --- */
  // the wire out of a step says what lets the run go on
  const past = s => !s ? 'every box ticked' : s.mode === 'check' ? 'either verdict' : 'step-done';

  function issue(s, after = []){
    const chain = ['task', ...after.map((_, i) => 'after' + i), 'review'];
    const name = k => (s[k] || {}).who || 'the agent';
    return {
      intro: 'You add the rookery:trigger label to an issue. Nothing runs before that.',
      nodes: [
        {id: 'trigger', lane: 0, title: 'Label the issue', label: 'rookery:trigger', tone: 'live',
          note: 'Someone with write access adds the label. No polling, no autonomous pickup.'},
        {id: 'plan', lane: 1, title: 'Plan', label: 'rookery:planning',
          note: 'Reads the repo and writes `plan.md`, `tasks.md` and `decisions.md` into `agent-plans/<n>/`. Anything else it touches is reverted.', ...s.plan},
        {id: 'answer', lane: 0, same: true, title: 'Answer questions', label: 'rookery:awaiting-input', tone: 'live',
          note: 'When the plan needs information, the question comes as a comment. Your reply goes back to the same planning session.'},
        {id: 'approve', lane: 0, title: 'Approve the plan', label: 'rookery:awaiting-approval', tone: 'live',
          note: 'Comment `approve`, matched as text, from someone with write access. Never the portal, never a model.'},
        {id: 'task', lane: 1, title: 'Work the tasks', label: 'rookery:implementing', loop: '↻ fresh session per task',
          note: 'A fresh session per task, so task 15 costs what task 1 did. `decisions.md` is the only memory between tasks.',
          again: 'next unticked task → `task-done` → Rookery ticks and commits · at most 15', ...s.task},
        ...after.map((a, i) => ({id: 'after' + i, lane: 1, label: 'rookery:implementing', ...a})),
        {id: 'review', lane: 1, title: 'Review the diff', label: 'rookery:reviewing',
          note: 'Reads the whole change once. Advisory: its verdict flags the run, it never blocks the push.', ...s.review},
        {id: 'push', lane: 2, title: 'Push and open a draft PR',
          note: 'To `feature/issue-<n>`, with the plan inside. The agent never pushes, and nothing merges.'},
        {id: 'wanted', lane: 0, title: 'Changes wanted', label: 'rookery:changes-wanted',
          note: '`changes-needed` from the reviewer or a check. Comment `approve` again to run them again.'},
        {id: 'done', lane: 2, same: true, title: 'Done', label: 'rookery:done', tone: 'ok',
          note: 'A human reviews the draft pull request and merges it.'},
      ],
      edges: [
        ['trigger', 'plan', 'webhook'], ['plan', 'answer', '', 'loop'], ['answer', 'plan', '', 'loop'],
        ['plan', 'approve', 'STATUS: plan-ready'], ['approve', 'task', 'approve'], ['task', 'task', '', 'loop'],
        ...chain.slice(1).map((k, i) => [chain[i], k, past(after[i - 1])]),
        ['review', 'push', 'either verdict'], ['push', 'done', 'review-ok'], ['push', 'wanted', 'changes-needed', 'alt'],
        ['wanted', 'approve', 'approve again', 'loop'],
      ],
      run: [
        ['trigger', 'plan', `The label sends a webhook. ${name('plan')} reads the issue and the repo.`],
        ['plan', 'answer', 'The plan needs information, so a question goes to the issue as a comment.'],
        ['answer', 'plan', 'Your reply goes back to the same planning session.'],
        ['plan', 'approve', 'The plan and the task list are ready. They wait for your approve comment.'],
        ['approve', 'task', 'approve starts task 1 in a fresh session.'],
        ['task', 'task', 'Task 1 is done. Rookery ticks it, commits it, and starts task 2 in a new session.'],
        ['task', 'task', 'Task 2 is done. Each task gets a new session, so the cost stays flat.'],
        ...chain.slice(1).map((k, i) => [chain[i], k, k === 'review'
          ? `${name('review')} reads the full change once. Its verdict is advisory.`
          : `${i ? 'Next' : 'Every task is ticked. Then'}, ${after[i].who || 'a step'} runs its ${after[i].mode}.`]),
        ['review', 'push', 'Rookery pushes feature/issue-<n> and opens a draft pull request.'],
        ['push', 'done', 'rookery:done. A human reviews the pull request and merges it.'],
      ],
    };
  }

  function pr(s, user = 'rookery-agent'){
    return {
      intro: `Someone with write access asks @${user} for a review.`,
      nodes: [
        {id: 'ask', lane: 0, title: 'Ask for a review', label: '@' + user, tone: 'live',
          note: `Mention @${user} in a comment on a pull request (the rest of the comment is your note), add rookery:pr-review, or request its review.`},
        {id: 'prr', lane: 1, title: 'Review the pull request', label: 'rookery:pr-review',
          note: 'A fresh session on the PR head. Read-only, with the config from base like every job. Forks are refused.', ...s},
        {id: 'post', lane: 2, title: 'Post the review',
          note: 'A comment review with a verdict and a findings table. Advisory: it never approves and never blocks a merge.'},
      ],
      edges: [['ask', 'prr', 'webhook'], ['prr', 'post', 'verdict']],
      run: [['ask', 'prr', `${s.who || 'The reviewer'} reads the pull request in a fresh session.`],
        ['prr', 'post', 'Rookery posts the review. Ask again to review again.']],
    };
  }

  const LOOP = issue({
    plan: {who: 'rk:planmaker', model: 'opus'},
    task: {who: 'rk:implementer', model: 'sonnet'},
    review: {who: 'rk:reviewer', model: 'opus'},
  }, [{title: 'Security check', who: 'rk:sentry', model: 'opus', mode: 'check', tag: 'optional · check',
    note: 'An `after` step a repo adds in `.rookery/workflow.json`. Read-only: its findings go into the PR body, and it never blocks the push.'}]);
  const PR = pr({who: 'rk:pr-reviewer', model: 'opus'});

  return {draw, card, issue, pr, SPECS: {loop: LOOP, pr: PR}};
})();
