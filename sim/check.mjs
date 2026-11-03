// node sim/check.mjs: holds the simulator's flow to agent.py. Run it after changing the flow. It reads agent.py as
// text, so a renamed phase or a new deny rule fails here instead of drifting on the page.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {buildRun, DENY, MAX_STEPS, MAX_TASKS, PHASES, RUN_CAP, stateAt, TRIGGER, USD_CAP} from './factory-shared/run.js';

const src = fs.readFileSync(new URL('../agent.py', import.meta.url), 'utf8');
const list = src.match(/^PHASES = \[([\s\S]*?)\]/m)[1].match(/"([^"]+)"/g).map((s) => s.slice(1, -1));
assert.deepEqual(PHASES, list);
assert.equal(TRIGGER, src.match(/^TRIGGER = "(.+)"/m)[1]);
assert.equal(RUN_CAP, Number(src.match(/"RUN_CAP", "(\d+)"/)[1]));
assert.equal(USD_CAP, Number(src.match(/"USD_CAP", "(\d+)"/)[1]));
const deny = src.slice(src.indexOf('"--disallowed-tools"')).split('\n').slice(0, 2).join(' ').match(/"Bash\([^"]+"/g).map((s) => s.slice(1, -1));
assert.deepEqual(DENY, deny);
// the order implement() and publish() call things in
for (const [a, b] of [['after_steps(c, wt, after)', 'set_phase(c, "rookery:reviewing")'], ['set_phase(c, "rookery:reviewing")', '"git", "push"'], ['"git", "push"', '"pr", "create", "--draft"']]) {
  const fn = src.slice(src.indexOf('def publish('));
  assert.ok(fn.indexOf(a) >= 0 && fn.indexOf(a) < fn.indexOf(b), `${a} before ${b}`);
}

// a run: calls add up, ticks never go back, a hold is where it says, and every beat has its words
for (const scenario of ['clean', 'question', 'changes', 'failure'])
  for (const tasks of [1, 4, MAX_TASKS])
    for (const after of [0, MAX_STEPS]) {
      const r = buildRun({scenario, tasks, after});
      const plans = scenario === 'question' ? 2 : 1;
      const retry = scenario === 'failure' ? 1 : 0;
      assert.equal(r.calls, plans + tasks + retry + after + 1, `${scenario} calls`);
      assert.equal(r.commits, tasks);
      let ticks = 0;
      for (let t = 0; t <= r.dur; t += 0.05) {
        const s = stateAt(r, t);
        assert.ok(s.ticks >= ticks && s.ticks <= tasks);
        ticks = s.ticks;
        assert.ok(s.text && s.who && s.fn);
        assert.ok(Number.isFinite(s.ang));
        assert.ok(s.phase === TRIGGER || PHASES.includes(s.phase), s.phase);
      }
      assert.equal(ticks, tasks);
      assert.equal(stateAt(r, r.dur).prUp, 1);
      assert.equal(stateAt(r, r.dur).phase, scenario === 'changes' ? 'rookery:changes-wanted' : 'rookery:done');
      const kinds = r.holds.map((h) => h.kind).join();
      assert.equal(kinds, {clean: 'approve', changes: 'approve', question: 'reply,approve', failure: 'approve,resume'}[scenario]);
      for (const h of r.holds) assert.equal(stateAt(r, h.t).hold, h.kind);
      // the gate is shut until the approve hold, open after it
      const appr = r.holds.find((h) => h.kind === 'approve').t;
      assert.ok(stateAt(r, appr).gateShut);
      assert.equal(stateAt(r, appr + 1).gate, 1);
    }
console.log('ok');
