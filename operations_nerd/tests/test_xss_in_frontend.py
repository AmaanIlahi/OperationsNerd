"""
Smoke test for the XSS fix in frontend/index.html.

The old code used `innerHTML` with template strings in the pack picker,
validation error list, and the table renderers, interpolating untrusted
strings (pack display_name from YAML, server-side error detail, LLM
output) directly into HTML. A pack author who wrote
`display_name: "Real Estate </option><img src=x onerror=alert(1)>"`
in pack.yaml would have triggered script execution in every browser
that opened the picker.

The fix replaces those `innerHTML` sites with a small `el()` helper that
uses createElement + textContent + setAttribute, so strings are always
rendered as text.

This test simulates the threat by writing a pack folder with a malicious
display_name, loading the picker endpoint, and verifying the malicious
string is returned verbatim. The actual DOM-rendering safety lives in
the JS using textContent; the static check at the end confirms the
`innerHTML` keyword is gone from the frontend source.
"""

import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from packs.loader import load_pack
from db import db as d
from main import app


MALICIOUS_DISPLAY_NAME = 'Evil Pack </option><img src=x onerror="window.__pwned=true">'


# Write a malicious pack into a temp directory and point the loader at it.
tmp = tempfile.mkdtemp(prefix="opsnerd_xss_test_")
try:
    pack_dir = os.path.join(tmp, "evil")
    os.makedirs(pack_dir)
    with open(os.path.join(pack_dir, "pack.yaml"), "w") as f:
        # Single-quoted YAML so the double-quotes inside the malicious
        # display_name don't terminate the scalar.
        f.write(f"id: evil\nversion: 0.1.0\ndisplay_name: '{MALICIOUS_DISPLAY_NAME}'\n")
    # The other files can be empty placeholders -- we never call process_event
    # in this test, so the empty prompts/event_schemas/questionnaire are fine.
    for fname in ("questionnaire.yaml", "event_schemas.yaml", "prompts.yaml", "approval_defaults.yaml"):
        with open(os.path.join(pack_dir, fname), "w") as f:
            f.write("")

    print("Testing that the loader accepts a pack with HTML in display_name...")
    pack = load_pack("evil", packs_dir=tmp)
    assert pack.display_name == MALICIOUS_DISPLAY_NAME, "loader should pass the string through verbatim"
    print(f"  display_name: {pack.display_name!r}")

    print("\nTesting that /packs returns the string verbatim to the frontend...")
    # Use list_packs pointing at our temp dir.
    from packs import loader as pack_loader
    original_default = pack_loader.os.path.join(pack_loader.os.path.dirname(__file__))
    # We just need to confirm the JSON shape: the frontend will see this
    # exact string in `p.display_name` and the new createElement / textContent
    # code will render it safely.
    packs = pack_loader.list_packs(packs_dir=tmp)
    assert len(packs) == 1
    assert packs[0]["display_name"] == MALICIOUS_DISPLAY_NAME
    print(f"  /packs returns: {packs[0]!r}")

    # Now flip to the actual API: serve a request and confirm the body.
    d.init_db(reset=True)
    client = TestClient(app)
    # /packs lists subfolders of the default packs dir, not our tmp. We test
    # the loader-level round-trip above; the API just JSON-encodes whatever
    # the loader returns, so server-side escaping of the JSON output is
    # automatic. The actual XSS risk was in the *frontend* render path
    # (innerHTML), which this test documents as the threat model.
    resp = client.get("/packs")
    assert resp.status_code == 200
    # The default packs (realestate, healthclub) should be present.
    body = resp.json()
    print(f"  /packs API response: {[p['id'] for p in body]}")
    assert any(p["id"] == "realestate" for p in body)
    # No "evil" pack in the default list (it's only in our temp dir).
    assert not any(p["id"] == "evil" for p in body)

    print("\nThreat model documented:")
    print("  - Loader passes display_name verbatim (no escaping).")
    print("  - JSON wire format preserves the string as-is.")
    print("  - XSS prevention lives in the frontend's createElement/textContent")
    print("    path. Verifying the actual DOM behavior requires a browser test;")
    print("    the static check is that the JS no longer uses innerHTML for")
    print("    these two render sites (lines 137-149 and 240-256 of the new")
    print("    frontend/index.html).")

    # Final static check: grep for innerHTML in the frontend file to confirm
    # the XSS-bearing patterns are gone.
    frontend_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "frontend", "index.html",
    )
    with open(frontend_path) as f:
        frontend_src = f.read()
    assert "innerHTML" not in frontend_src, (
        "frontend still uses innerHTML somewhere -- this is the XSS vector"
    )
    print("  Static check: 'innerHTML' is no longer used in frontend/index.html. PASS")

    print("\nXSS frontend safety smoke test passed.")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
