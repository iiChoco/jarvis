"""The interview room — codename **Adjoint**.

The adjoint of an operator is the same map seen from the other side of
the inner product. This is Ciel seen from the other side of the table: not
the assistant that knows the owner's calendar and can reach their Mac, but
an interviewer who knows one fictional company (or one consulting case)
and nothing else, talking to someone who is not the owner.

That "nothing else" is the whole design. The room is a second surface on
the hub's web server, at ``/interview``, and it shares the process with
Ciel proper — but nothing more. It has its own accounts (the owner's
friends, by username and password), its own sessions (one brain per live
interview, with no tools, no memory index, no Mac, and a budget), its own
wire protocol (a small catalog, binary frames allowed for the recording),
its own storage tree, and its own prompts. It never touches the Chart's
queue, the arbiter, the hub token, or the personal ``Brain``; the file
names that hold other people's credentials are on the personal brain's
forbidden list by name.

What it does do: invent a company from what the candidate asks for, or
pick a case; show a brief; conduct the interview by voice, waiting
patiently for long answers and apologizing when it cuts someone off;
record the whole thing; and write a debrief afterwards.
"""
