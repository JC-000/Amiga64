"""A/B session logs for the replay tests (fix8): the writer of
build/evidence/w6-kbd-review7/test_fix7_attacks.py, adopted.  Every key /
raw / joystick line comes from the real Stream.poll (stubbed reads, a
1.15 s clock); the screen rows, heartbeat, answer and run end lines are
written as the live driver writes them; ab() replays the pair logs with
replay_report, as the operator script's --replay does."""

from __future__ import annotations

from tests import kbd_echo as ke

Q, UP = ke.K("q"), ke.K("^")
SEMI, SLASH, COMMA, L = ke.K(";"), ke.K("/"), ke.K(","), ke.K("l")
P1 = ke.poll_period(1.0)                 # 1.15 s
R6LOW = 0xFF & ~(1 << 6)
R7LOW = 0xFF & ~(1 << 7)


def state(keys=(), nkey=0, idle=0xFF, raw=None):
    return {"nkey": nkey, "idle": idle, "mat": ke.bits_mat(set(keys)),
            "raw": ke.bits_mat(set(raw if raw is not None else keys))}


def live(states, step, dt=P1):
    st = ke.replay_stream()
    st.nkey, st.mat, st.idle, st.raw_extra = 0, bytes(8), 0xFF, set()
    st.step, st.period = step, P1
    st.emit = lambda line: None
    it = iter(states)
    st.read_state = lambda: next(it)
    now = [0.0]
    real = ke.time.monotonic
    ke.time.monotonic = lambda: now[0]
    try:
        for _ in states:
            st.poll(screen=False)
            now[0] += dt
    finally:
        ke.time.monotonic = real
    return st


# --- session log writer -------------------------------------------------------

def hold_states(key, n=5, nk0=0):
    """Clean hold of *key* at n polls, then released."""
    return [state({key}, nk0 + 1)] * n + [state(set(), nk0 + 1)]


CLEAN1 = [state()] + hold_states(Q)
CLEAN2 = [state()] + hold_states(SEMI) + [state({SLASH}, 2)] * 5 + \
    [state(set(), 2)]
TAP1 = [state(), state({Q}, 1), state(set(), 1), state(set(), 1)]
PHANTOM1 = [state()] + [state({Q, UP}, 2)] * 5 + [state(set(), 2)]
# the marginal settle: Q commits alone, then UPARROW keeps flickering into
# raw (never 4 ms stable, so never committed) while Q stays held
UPRAW1 = [state(), state({Q}, 1), state({Q}, 1, raw={Q, UP}), state({Q}, 1),
          state({Q}, 1, raw={Q, UP}), state({Q}, 1), state(set(), 1)]
# the marginal settle, committed later: Q alone first, UPARROW commits
# two polls into the hold
UPLATE1 = [state(), state({Q}, 1), state({Q}, 1), state({Q, UP}, 2),
           state({Q, UP}, 2), state({Q}, 2), state(set(), 2)]
# step 2: / never committed, raw-only at 3 polls, row 7 low at idle in
# between (the idle-mask flicker of fix7 K3, on /'s row 7): settle-specific
# by fix7's own rule, and no brushed neighbour can produce it
SLASH_R7 = [state()] + hold_states(SEMI) + [
    state(set(), 1, raw={SLASH}), state(set(), 1, idle=R7LOW, raw=set()),
    state(set(), 1, raw={SLASH}), state(set(), 1, idle=R7LOW, raw=set()),
    state(set(), 1, raw={SLASH}), state(set(), 1)]


class Log:
    def __init__(self):
        self.lines = []

    def run(self, name, s1, s2, poll=1.0, answer="yes", fixed_rest=True):
        clock = [0.0]
        out = self.lines

        def at(msg, t=None):
            out.append(f"[{clock[0] if t is None else t:7.2f}] {msg}")
        control = name.startswith("nosettle")
        at(f"{name} started (--poll {poll:g}s): operator, follow the C64 "
           "screen" + ("  *** CONTROL: SUPPOSED TO SHOW THE DEFECT ***"
                       if control else ""))
        st = ke.replay_stream()
        st.nkey, st.mat, st.idle, st.raw_extra = 0, bytes(8), 0xFF, set()
        st.t0 = 0.0
        st.out = lambda line: out.append(line)
        real = ke.time.monotonic
        ke.time.monotonic = lambda: clock[0]
        period = ke.poll_period(poll)

        base = [0]

        def polls(states):
            # nkey keeps counting across steps (as hle_con_nkey does)
            it = iter([dict(x, nkey=(x["nkey"] + base[0]) & 0xFF)
                       for x in states])
            st.read_state = lambda: next(it)
            base[0] += max(x["nkey"] for x in states)
            for _ in states:
                clock[0] += period
                st.poll(screen=False)
                at(f"hb polls +1 max gap {period:.2f}s")
        try:
            for n, states in ((1, s1), (2, s2)):
                clock[0] += 0.05
                at(f"  | STEP {n}/9: {ke.STEPS[n - 1].title}")
                at("  | >")
                polls(states)
                clock[0] += 0.05
                at(f"  | STEP {n} PASS")
            steps = "PP-------"
            if not control:
                for n in range(3, 10) if fixed_rest else ():
                    clock[0] += 0.05
                    at(f"  | STEP {n}/9: {ke.STEPS[n - 1].title}")
                    at("  | >")
                    polls([state()] * 2)
                    at(f"  | STEP {n} SKIP")
                steps = "PPSSSSSSS"
            at(f"  | STEPS {steps}")
            if control:
                for n in (1, 2):
                    at(f"operator: step {n} held as asked (x)? {answer}")
            verdict = "INCONCLUSIVE (want DEFECT SHOWN)" if control else \
                "INCOMPLETE (want PASS)"
            at(f"{name}: {verdict}, STEPS {steps}")
        finally:
            ke.time.monotonic = real
        return self

    def session(self, plan, poll=1.0, **runs):
        self.lines.append(f"[10:00:00] kbd_echo MANUAL on 192.0.2.1, runs "
                          f"{plan.replace(',', ', ')}, poll {poll:g}s, prg x")
        for name in plan.split(","):
            s1, s2 = runs.get(name.split("@")[0], (CLEAN1, CLEAN2))
            self.run(name, s1, s2, poll)
        return self

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n")
        return str(path)


def plan(i):
    return ke.AB_PLANS[i % 2]


def ab(tmp_path, sessions, extra_paths=()):
    """sessions: [(poll, {build: (s1, s2)})] -> the A/B line, and SUMMARY."""
    paths = []
    for i, (poll, runs) in enumerate(sessions):
        paths.append(Log().session(plan(i), poll, **runs)
                     .write(tmp_path / f"pair{i + 1}" / "session.log"))
    out = []
    ke.replay_report(paths + [paths[k] for k in extra_paths],
                     out=out.append)
    line = [l for l in out if l.startswith("A/B")]
    assert len(line) == 1, out[-5:]
    return line[0].split("'): ", 1)[1], out


CLEAN = (1.0, {})
