# Phase D — manual browser checklist

Page: `/app`. Run the server from `operations_nerd/` and open http://127.0.0.1:8000/app.

```
set SIGNUP_INVITE_CODE=let-me-in
set ANTHROPIC_API_KEY=...        (needed for the chat steps)
uvicorn main:app --reload
```

Tick each step. "Expect" says what you should see.

## 1. Sign up and log in
- [ ] Open `/app`. **Expect:** Log in / Sign up screen.
- [ ] Sign up with a wrong invite code. **Expect:** red "Invalid invite code", still on the form.
- [ ] Sign up with the right code. **Expect:** you land on "New business"; your email shows top right.
- [ ] Log out, then log in with a wrong password. **Expect:** "Wrong email or password".
- [ ] Log in correctly. **Expect:** you are back in the app.

## 2. Create a business from a template
- [ ] Name "Sunshine Fitness", template **Health club**, Create.
  **Expect:** tabs Location / Member / Lead / Class; header shows "version 1".
- [ ] Open the **History** button. **Expect:** one row, `v1`, source `template`, "current". Click it to see the full spec read-only. Back to CRM.

## 3. Records and links
- [ ] Location tab → New Location → name "Downtown" → Create. **Expect:** row appears.
- [ ] Member tab → New Member → press Create with nothing filled in.
  **Expect:** "Full name is required" under the field (fields marked `*` are required).
- [ ] Fill Full name "Ann Lee", choose Home location "Downtown", type "twelve" in Notes → Create.
  **Expect:** row shows Ann Lee with Downtown in the Home location column.
- [ ] Note the line "“Attends” link is not editable yet" (many-to-many links are not editable in v1).
- [ ] Edit Ann Lee, change the location, Save. **Expect:** the row updates.

## 4. Chat: add a field, approve
- [ ] In the chat type: `add a referral source field to leads`. **Expect:** spinner, then a reply and a
  **Proposed changes** card: "Add a text field “Referral source” to “Lead”" with an impact line.
- [ ] Click **Approve**. **Expect:** badge "Applied as version 2"; Lead tab now has a Referral source column; header "version 2".
- [ ] Send another message. **Expect:** the approved card still says Applied (no Approve button comes back).

## 5. Change a field's type with existing data
- [ ] Chat: `make the Notes field on members a number`.
  **Expect:** card says "Change “Member” → “Notes” to type number", impact: "0 of 1 stored value convert cleanly" and "1 value will not fit (kept, flagged…)" plus the sample "twelve".
- [ ] Approve. Open the Member tab. **Expect:** Ann Lee's Notes cell is highlighted and a "⚠ 1 value(s) don’t fit: Notes" badge shows. The value is still there.
- [ ] Edit Ann Lee. **Expect:** a note under Notes: stored value “twelve” doesn’t fit; kept unless you change it. Save a different field → "twelve" is still stored.

## 6. Revert
- [ ] History → click `v1` → **Revert to this version** → **Yes, revert**.
  **Expect:** "Reverted to version 1. This created version 4." List now has v1…v4, v4 source `revert`.
- [ ] Back to CRM. **Expect:** no Referral source column; Ann Lee is still in Member (records are never touched by revert).

## 7. Bad request
- [ ] Chat: `add another entity called Member, and give leads a field of type teleport`.
  **Expect:** a card with no Approve button, headed "No changes proposed", and a **Not proposed** list with reasons
  (duplicate entity label / unsupported field type). History still ends at v4.

## 8. Stale proposal
- [ ] Ask for two different field additions in two separate messages without approving the first.
- [ ] Approve the second one, then the first. **Expect:** the first shows "Out of date" and a message telling you to ask again.

## 9. Failure and slow calls
- [ ] Unset `ANTHROPIC_API_KEY`, restart, send a message. **Expect:** a red error and a **Try again** button; no crash.
- [ ] With a key set and a slow reply, after about 12 s the spinner text changes to "Still working…".

## 10. Empty business
- [ ] New business "Iron Works", template **Empty**. **Expect:** "No entity types yet…" hint.
- [ ] Chat: `I run a gym with members and leads`. Approve. **Expect:** tabs appear after Approve without a page reload.

## 11. Isolation
- [ ] Log out, sign up as a second account. **Expect:** no businesses from the first account; opening
  `/api/businesses/1` in the address bar returns 404.
