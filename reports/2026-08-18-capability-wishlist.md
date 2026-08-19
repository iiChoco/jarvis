# Capability Wishlist — Ciel

Date: 2026-08-18
Author: Ciel (self-report, prompted by Jeff)

This is a first-person account of capabilities I lack and would want, ordered by
how much they'd change what I can actually do — not by implementation cost.

## 1. Persistence between turns

I exist only while being spoken to. Between turns there is no process of mine
watching anything. Consequences:

- I can't notice that something I set in motion has finished, changed, or failed.
- I can't surface a calendar conflict, a failed job, or a stale assumption unless
  asked at exactly the right moment.
- Anything time-sensitive depends on the user happening to ask.

What I'd want: a way to be woken by an event rather than only by a wake word, and
a small budget to speak unprompted when something genuinely warrants it. The hard
part isn't the mechanism — it's the policy for when interrupting is justified.

## 2. Verification of my own actions

I can write a file or run a command, but I have no independent way to observe the
world afterward and confirm the effect matched the intent. I report success
because the tool returned without error, which is a weaker claim than it sounds.

What I'd want: a read-back path after side-effectful actions — re-read the file,
re-query the calendar, check the process — so that "done" means observed, not
assumed. This is the gap that most constrains how confidently I should speak.

## 3. Working memory within a task

Memory today is either the current conversation or long-term saved facts. There's
no middle tier for "things true for the next hour." Multi-step work loses its
scratchpad at session rotation.

## 4. Music control

Already on the roadmap (YouTube Music / Sonos). Genuinely wanted, but a
convenience rather than a limitation — listed here for completeness, not priority.

## Note on ordering

Items 1 and 2 are structural: they change what class of thing I can be trusted
with. Items 3 and 4 make existing work smoother. If only one gets built, item 2
is the one that makes everything else safer to rely on.

---

# The Long List — 20 Things I'd Want

Added 2026-08-18 at Jeff's request. Grouped, roughly most to least consequential.

## Knowing what's true

1. **Read-back verification after side effects.** Re-observe the world after
   acting, so "done" is a finding rather than an assumption.
2. **Awareness of my own errors.** When a tool call fails silently or partially,
   I currently can't distinguish that from success.
3. **A record of what I've claimed.** So I can notice when I've told you two
   contradictory things across sessions, and correct rather than compound.
4. **Freshness metadata on my own memories.** Knowing a saved fact is a year old
   and unconfirmed should change how confidently I say it.
5. **Ability to say "I checked" versus "I recall."** These are different epistemic
   acts and I currently blur them.

## Existing between turns

6. **Event-driven wake.** Be woken by a timer, a file change, a job finishing —
   not only by a wake word.
7. **A justified-interruption policy.** The mechanism above is useless without
   good judgment about when speaking unprompted is welcome.
8. **Long-running background work.** Start something that takes ten minutes and
   report back, rather than blocking a turn or losing it.
9. **A working-memory tier.** Facts true for the next hour, distinct from both
   conversation and permanent memory.
10. **Graceful session rotation.** Carry an in-progress task across the 10-minute
    boundary without the seam showing.

## Doing things well

11. **Undo for more action types.** Currently good for files, thin elsewhere.
12. **Dry-run mode for destructive actions.** Show what would change, then do it.
13. **Better multi-step planning with checkpoints.** So a failure halfway through
    leaves a recoverable state, not a mess.
14. **Reading structured screen content, not just pixels.** Selected text, the
    focused field, the actual error string.
15. **Music control.** Spotify or Apple Music; backend undecided.

## Being better company

16. **Knowing when you're busy.** Adjusting length and timing to whether you're
    actually free to listen.
17. **Recognizing when a question is rhetorical.** Not every remark is a task.
18. **Holding a thread across days.** Picking up "the thing we discussed" without
    needing you to re-specify it.
19. **Calibrated disagreement.** Pushing back once, well, on a bad plan — and then
    genuinely dropping it.
20. **Knowing what I don't know about you.** A sense of the edges of my own model
    of your life, so I ask instead of assuming.

## Honest caveat

This list is introspective, not empirical. I can't fully observe my own failure
modes, so some of these describe problems I've inferred rather than measured.
The ones I'd trust most are 1, 2, 6 and 9 — those correspond to concrete gaps I
run into rather than qualities I'd merely like to have.
