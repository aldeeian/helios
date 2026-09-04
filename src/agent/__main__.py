"""Entry point: `python -m src.agent "your question"` runs one NL query.

Uses Claude if ANTHROPIC_API_KEY (or an `ant` profile) is available, otherwise
the offline stub generator. Prints the answer, the SQL that produced it, and
the tables it touched — every answer is auditable."""

from __future__ import annotations

import sys

from . import nl_agent


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python -m src.agent "your question"')
        raise SystemExit(2)
    question = " ".join(sys.argv[1:])
    ans = nl_agent.ask(question)
    print(f"Q: {ans.question}")
    print(f"[{ans.llm}, {ans.attempts} attempt(s)]")
    if ans.ok:
        print(f"\nAnswer: {ans.summary()}")
        print(f"\nSQL:\n{ans.sql}")
        print(f"\nGrounded in: {', '.join(ans.tables_used)}")
    else:
        print(f"\nCould not answer safely: {ans.error}")


if __name__ == "__main__":
    main()
