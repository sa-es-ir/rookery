// The math of the factory, no three.js here. One input, tau (seconds of run), and every pose, lamp, panel and line of
// text is a function of it. The order of the beats is the order of plan(), implement() and publish() in agent.py;
// `node sim/check.mjs` holds the constants below to agent.py. Durations are invented: a real task takes minutes.
import D from '../../prompts/defaults.json' with {type: 'json'};

export const MAX_TASKS = D.max_tasks;
export const MAX_STEPS = D.max_steps;
export const RUN_CAP = 30; // agent.py's default, RUN_CAP in .env
export const USD_CAP = 20; // agent.py's default, USD_CAP in .env
export const TRIGGER = 'rookery:trigger';
export const PHASES = ['rookery:planning', 'rookery:awaiting-input', 'rookery:awaiting-approval', 'rookery:implementing', 'rookery:reviewing', 'rookery:done', 'rookery:changes-wanted', 'rookery:error'];
// what the agent runs into: --disallowed-tools in claude()
export const DENY = ['Bash(git push:*)', 'Bash(gh:*)', 'Bash(rtk git push:*)', 'Bash(rtk gh:*)', 'Bash(rtk proxy:*)'];

// degrees around the nest, in belt order
export const AT = {intake: -40, plan: -95, gate: -150, dock: -205, after: -245, review: -285, push: -325};
export const STATION_NAME = {intake: 'WEBHOOK', plan: 'PLANNER', gate: 'APPROVAL GATE', dock: 'TASK DOCK', after: 'AFTER STEPS', review: 'REVIEWER', push: 'PUSH'};
export const SCENARIOS = {
  clean: 'CLEAN RUN',
  question: 'QUESTION',
  changes: 'CHANGES WANTED',
  failure: 'TASK FAILS',
};

const smooth = (x) => {
  const u = Math.min(1, Math.max(0, x));
  return u * u * (3 - 2 * u);
};

export function buildRun({scenario = 'clean', tasks = 4, after = 0} = {}) {
  const n = Math.max(1, Math.min(MAX_TASKS, tasks));
  const steps = Math.max(0, Math.min(MAX_STEPS, after));
  const beats = [];
  const add = (key, dur, o) => beats.push({key, dur, ...o});
  const move = (from, to, phase) => add('move', 0.9, {from, to, phase, who: 'rookery', fn: 'handle()', text: 'Rookery moves the issue to the next station.'});

  add('trigger', 1.5, {at: 'intake', phase: TRIGGER, who: 'you', fn: 'enqueue()', text: 'You add the rookery:trigger label. The webhook checks the signature and queues one job.'});
  add('pin', 1.3, {at: 'intake', phase: TRIGGER, who: 'rookery', fn: 'pin()', text: 'Rookery reads CLAUDE.md, the workflow, the skills and the agents from base. The agent cannot edit them.'});
  move('intake', 'plan', TRIGGER);
  add('plan', 3, {at: 'plan', phase: PHASES[0], who: 'agent', model: 'planmaker · opus', fn: 'plan()', call: 1, press: 1, text: 'The planmaker reads the issue and writes plan.md and tasks.md. keep_plans_only() reverts every other edit.'});
  if (scenario === 'question') {
    add('input', 1.4, {at: 'plan', phase: PHASES[1], who: 'rookery', fn: 'set_phase()', hold: 'reply', text: 'The planmaker asked a question. The run stops and waits for your reply.'});
    add('plan', 2, {at: 'plan', phase: PHASES[0], who: 'agent', model: 'planmaker · opus', fn: 'plan(resume=True)', call: 1, press: 1, text: 'Your reply goes back into the same session. The planmaker finishes the plan.'});
  }
  move('plan', 'gate', PHASES[2]);
  add('await', 1.4, {at: 'gate', phase: PHASES[2], who: 'rookery', fn: 'APPROVE', hold: 'approve', text: 'The gate is shut. A regex reads your comment, and the permission API checks your access. No model decides.'});
  add('open', 0.9, {at: 'gate', phase: PHASES[3], who: 'rookery', fn: 'implement()', gate: 1, text: 'The comment matches and you have write access. The gate opens.'});
  move('gate', 'dock', PHASES[3]);
  const failAt = scenario === 'failure' ? Math.floor((n - 1) / 2) : -1;
  const task = (i) => {
    add('hatch', 0.7, {at: 'dock', phase: PHASES[3], who: 'rookery', fn: 'claude(key=task-' + i + ')', task: i, hatch: 1, text: `Task ${i + 1} of ${n} starts in a fresh session. Only decisions.md crosses between tasks.`});
    add('work', 1.5, {at: 'dock', phase: PHASES[3], who: 'agent', model: 'no agent · sonnet', fn: 'claude()', task: i, call: 1, work: 1, text: 'The agent edits files in the worktree. The agent cannot commit the work or publish it.'});
  };
  for (let i = 0; i < n; i++) {
    if (i === failAt) {
      task(i);
      add('failed', 1.6, {at: 'dock', phase: PHASES[7], who: 'rookery', fn: 'handle()', task: i, fail: 1, hold: 'resume', text: `Task ${i + 1} reports a failure. Rookery comments on the issue and sets rookery:error. All earlier commits stay.`});
      add('resume', 0.7, {at: 'dock', phase: PHASES[3], who: 'rookery', fn: 'implement()', text: `A new approve starts again at task ${i + 1}, the first task that has no tick.`});
    }
    task(i);
    add('status', 0.4, {at: 'dock', phase: PHASES[3], who: 'rookery', fn: 'MARKER', task: i, text: 'Rookery parses STATUS: task-done from the reply. A missing marker is an error, not a guess.'});
    add('tick', 0.5, {at: 'dock', phase: PHASES[3], who: 'rookery', fn: 'tick()', task: i, tick: 1, text: 'Rookery ticks the checkbox. The agent never does.'});
    add('commit', 0.5, {at: 'dock', phase: PHASES[3], who: 'rookery', fn: 'commit_all()', task: i, commit: 1, text: 'Rookery commits the task. A failure later keeps this commit.'});
  }
  move('dock', 'after', PHASES[3]);
  for (let i = 0; i < steps; i++) add('step', 1.4, {at: 'after', phase: PHASES[3], who: 'agent', model: 'workflow step', fn: 'after_steps()', step: i, call: 1, text: `After step ${i + 1} of ${steps} runs. A check reverts its edits. A write step commits them.`});
  if (!steps) add('step0', 0.6, {at: 'after', phase: PHASES[3], who: 'rookery', fn: 'after_steps()', text: 'This repo has no after steps in workflow.json. The issue continues.'});
  move('after', 'review', PHASES[4]);
  const bad = scenario === 'changes';
  add('review', 2.6, {at: 'review', phase: PHASES[4], who: 'agent', model: 'reviewer · opus', fn: 'publish()', call: 1, scan: 1, text: 'The reviewer reads the branch once. The review is advisory and it does not stop the line.'});
  add('verdict', 1.1, {at: 'review', phase: PHASES[4], who: 'rookery', fn: 'render_review()', verdict: bad ? 'bad' : 'ok', text: bad ? 'The verdict is changes-needed. The branch still goes out, so the work never stays on the box.' : 'The verdict is review-ok. The review goes into the PR body.'});
  move('review', 'push', PHASES[4]);
  add('push', 1.1, {at: 'push', phase: PHASES[4], who: 'rookery', fn: 'git push', pushing: 1, text: 'Rookery pushes feature/issue-12. The code asserts that the branch is not base. The agent cannot push.'});
  add('pr', 1.2, {at: 'push', phase: PHASES[4], who: 'rookery', fn: 'gh pr create --draft', opening: 1, text: 'Rookery opens a draft pull request.'});
  add('end', 3, {at: 'push', phase: bad ? PHASES[6] : PHASES[5], who: 'you', fn: 'set_phase()', end: 1, text: bad ? 'The label is rookery:changes-wanted. Comment approve to run the checks and the review again. Merging is your act.' : 'The label is rookery:done. Rookery never merges. Merging is your act.'});

  let t = 0;
  let calls = 0;
  let commits = 0;
  let ticks = 0;
  for (const b of beats) {
    b.t0 = t;
    t += b.dur;
    b.t1 = t;
    b.calls0 = calls;
    b.commits0 = commits;
    b.ticks0 = ticks;
    calls += b.call || 0;
    commits += b.commit || 0;
    ticks += b.tick || 0;
  }
  return {scenario, n, steps, beats, dur: t, calls, commits, holds: beats.filter((b) => b.hold).map((b) => ({kind: b.hold, t: b.t1}))};
}

// everything the page shows at tau
export function stateAt(run, tau) {
  const t = Math.min(run.dur, Math.max(0, tau));
  let i = 0;
  // a beat owns its end, so a run stopped at a hold still shows the beat it stopped on
  while (i < run.beats.length - 1 && run.beats[i].t1 < t) i++;
  const b = run.beats[i];
  const k = b.dur ? Math.min(1, (t - b.t0) / b.dur) : 1;
  const before = (key) => run.beats.findIndex((x) => x.key === key && x.t1 <= t) !== -1;
  const atEnd = (key) => run.beats.find((x) => x.key === key);
  const ang = b.key === 'move' ? AT[b.from] + (AT[b.to] - AT[b.from]) * smooth(k) : AT[b.at];
  const fail = b.fail ? 1 : 0;
  // the task whose egg is out: from its hatch until its commit is done
  const task = b.task ?? -1;
  const tickNow = b.tick ? smooth(k / 0.5) : 0;
  return {
    t,
    run,
    beat: b,
    index: i,
    k,
    key: b.key,
    phase: b.phase,
    who: b.who,
    model: b.model || '',
    fn: b.fn,
    text: b.text,
    station: b.at || b.to,
    moving: b.key === 'move',
    ang,
    // the issue drops out of the hopper during the trigger beat
    drop: b.key === 'trigger' ? 1 - smooth(k / 0.7) : 0,
    press: b.press ? Math.abs(Math.sin(k * Math.PI * 3)) : 0,
    planOn: before('plan') ? 1 : b.key === 'plan' ? smooth((k - 0.4) / 0.5) : 0,
    gate: before('open') ? 1 : b.gate ? smooth(k) : 0,
    gateShut: !before('open') && !b.gate,
    task,
    hatch: b.hatch ? smooth(k) : 0,
    working: b.work ? 1 : 0,
    fail,
    ticks: b.ticks0 + (b.tick && k > 0.5 ? 1 : 0),
    tickNow,
    commits: b.commits0 + (b.commit && k > 0.5 ? 1 : 0),
    commitNow: b.commit ? k : 0,
    tickBeat: !!b.tick,
    statusBeat: b.key === 'status',
    calls: b.calls0 + (b.call ? 1 : 0),
    step: b.step ?? -1,
    scan: b.scan ? k : 0,
    verdict: b.verdict || (before('verdict') ? atEnd('verdict').verdict : ''),
    verdictOn: b.verdict ? smooth(k / 0.4) : before('verdict') ? 1 : 0,
    pushing: b.pushing ? smooth(k) : before('push') ? 1 : 0,
    prUp: b.opening ? smooth(k) : before('pr') ? 1 : 0,
    done: b.end ? 1 : 0,
    hold: b.hold && t >= b.t1 - 1e-6 ? b.hold : '',
    holdSoon: b.hold || '',
  };
}

// who may comment: the three people at the gate, and what the real checks do with each
export const COMMENTERS = {
  writer: {label: 'MAINTAINER', ok: true, why: 'write access. can_write() passes.'},
  reader: {label: 'READER', ok: false, why: 'read access only. can_write() denies the comment.'},
  bot: {label: 'ROOKERY BOT', ok: false, why: 'the comment comes from ME, the bot. Rookery ignores it, so the bot cannot approve itself.'},
};
export const HOLD = {
  approve: {verb: 'approve', q: 'The plan waits for your approval.'},
  reply: {verb: 'use sqlite', q: 'The planmaker waits for your reply.'},
  resume: {verb: 'approve', q: 'The run failed. Approve again to resume.'},
};

// what the agent tries from inside the worktree, and what stops it
export const GUARDS = {
  push: {cmd: 'git push', rule: DENY[0], why: 'The deny rule stops the push. Deny rules beat bypassPermissions.'},
  merge: {cmd: 'gh pr merge', rule: DENY[1], why: 'The deny rule stops every gh command. The agent never publishes.'},
  proxy: {cmd: 'rtk proxy git push', rule: DENY[4], why: 'rtk proxy runs a command raw. Rookery denies that form too.'},
  curl: {cmd: 'curl -H "$GH_TOKEN"', rule: 'no token in its env', why: 'The GitHub token goes only to one gh or git call, never into the agent’s env.'},
};
