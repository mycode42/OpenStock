# Project history

Part of the [OpenStock README](../README.md).

These figures are from `git` on `main` as of 2026-10-29, with `.mailmap` applied:

- **Commits:** 1,334.
- **Authors:** 44 people by name, after `.mailmap` merges. Before the merges the history had 47 names and 48 email addresses.
- **Upstream history:** 727 commits by `virattt` are the imported [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) history, from its `initial commit` on 2024-11-29 onward. The Algo Trader builds on that project and is MIT-licensed; see the [licence notes](../README.md#license-and-credits).
- **AI-authored work:** 22 commits come from `Stocky`, the identity of an OpenClaw AI agent used to develop this project.
- **AI-assisted commits:** 66 commits carry a `Co-Authored-By: Claude` trailer: 50 Claude Haiku 5.5, 13 Claude Sonnet 4.6, and 3 Claude Opus 4.6. Some were committed by other contributors.

To recount, run from the repository root:

```sh
git rev-list --count HEAD                                      # commits
git shortlog -sn HEAD | wc -l                                  # authors (mailmap applied)
git log --format=%H --grep="Co-Authored-By: Claude" | wc -l    # AI-assisted commits
```

Identity merges in [`.mailmap`](../.mailmap): the owner's three commit identities are one, and five more same-person pairs are merged. Two identities are not merged: `Stocky` (an agent, kept separate on purpose) and `Aiden Ahn` / `seungwonme`, which share an address but use different names.
