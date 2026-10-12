# Webhook signature validation: 20 paired offline runs

Base: ba79e4f1000f3e5249fd6e284215c4285567fd08, existing PR10 head. A non-ASCII signature reaches hmac.compare_digest and raises TypeError instead of returning False. The webhook caller therefore cannot return its intended 401 rejection. Validate the 64-character hexadecimal digest before comparison; valid upper-case hex is normalized without changing the digest.

Each row executes the actual verify_signature AST in two fresh processes. Nine cases cover valid hex, sha256 prefix, surrounding whitespace, upper-case hex, non-ASCII, non-hex, wrong length, empty and incorrect digest. No external services. Full offline entrypoint: 41 pass (39 existing + 2 new). No live provider/deployment claim.

Reproduce: python3 scripts/prove_signature.py; scripts/test_offline.py with the project's test dependencies.

| Run | Before cases | After cases | Before non-ASCII | After non-ASCII |
| --- | --- | --- | --- | --- |
| 1 | 7/9 | 9/9 | TypeError | False |
| 2 | 7/9 | 9/9 | TypeError | False |
| 3 | 7/9 | 9/9 | TypeError | False |
| 4 | 7/9 | 9/9 | TypeError | False |
| 5 | 7/9 | 9/9 | TypeError | False |
| 6 | 7/9 | 9/9 | TypeError | False |
| 7 | 7/9 | 9/9 | TypeError | False |
| 8 | 7/9 | 9/9 | TypeError | False |
| 9 | 7/9 | 9/9 | TypeError | False |
| 10 | 7/9 | 9/9 | TypeError | False |
| 11 | 7/9 | 9/9 | TypeError | False |
| 12 | 7/9 | 9/9 | TypeError | False |
| 13 | 7/9 | 9/9 | TypeError | False |
| 14 | 7/9 | 9/9 | TypeError | False |
| 15 | 7/9 | 9/9 | TypeError | False |
| 16 | 7/9 | 9/9 | TypeError | False |
| 17 | 7/9 | 9/9 | TypeError | False |
| 18 | 7/9 | 9/9 | TypeError | False |
| 19 | 7/9 | 9/9 | TypeError | False |
| 20 | 7/9 | 9/9 | TypeError | False |
