# Deploy reconciliation — security hardening (branch `fix/security-hardening-aug2026`)

**Date:** 2026-08-16
**Author:** hardening pass off a full multi-agent RBAC/security audit of all six roles.

## ⚠️ READ THIS BEFORE DEPLOYING — the VM is AHEAD of this branch

The production VM (`/srv/brightwavehabitat/app/app.py`, bind-mounted) has work that was
edited **directly on the host** and **never committed to git**, including the **July 26**
hardening:

- `escapeHtml` XSS fix
- PA "Assistant Desk" (real PA role, tabbed desk, badges, notes, WhatsApp)
- Unified 6-stage inquiry pipeline (CEO side)
- Some stats/upload gating and CEO-only delete tweaks

**None of that is in git.** This branch was built on the July 25 GitHub copy plus the
uncommitted caution-fee/Resident-T&C work found locally. Therefore:

> ### DO NOT blind-push this branch onto the VM.
> A straight overwrite would REVERT the July 26 production fixes (PA desk, unified
> pipeline, etc.) that exist only on the VM.

Deploy is a **reconcile**, not an overwrite.

## Reconciliation procedure (when VM SSH access is restored)

> Note: SSH to the VM is currently blocked — all ports filtered from the work machine,
> almost certainly a fail2ban/whole-IP ban triggered by a failed key auth. Unban from the
> provider console (`fail2ban-client unban <ip>`) before starting.

1. **Snapshot the live tree first** (per standing rule — never wipe live state):
   ```bash
   cd /srv/brightwavehabitat/app
   cp app.py app.py.vm-$(date +%Y%m%d)         # back up the authoritative prod file
   cp data/brightwave.db data/brightwave.db.bak-$(date +%Y%m%d)   # PROD DB IS SQLITE here
   git status && git stash list
   ```
2. **Commit the VM's uncommitted work to a branch** so git finally reflects production:
   ```bash
   git checkout -b vm-live-$(date +%Y%m%d) && git add -A && git commit -m "Snapshot VM live tree (July 26 work: escapeHtml, PA desk, unified pipeline, ...)"
   ```
3. **Diff** the VM live tree against this hardening branch to see what the VM already has
   vs. what is genuinely new here. The commits below are intentionally **granular by theme**
   so you can cherry-pick only what the VM lacks:
   ```bash
   git fetch && git diff vm-live-YYYYMMDD..origin/fix/security-hardening-aug2026 -- app.py
   ```
4. **Cherry-pick / hand-merge** the fixes the VM does not already have. For a fix the VM
   already implemented differently (e.g. escapeHtml), keep the VM's version — do not
   double-apply.
5. **Deploy**: app.py is bind-mounted, so `edit host file + docker restart` — no image
   rebuild (see the `vps_bind_mounts` note).
6. **DB**: the new `login_audit` table auto-creates via `db.create_all()` on boot. Verify it
   appears (`.tables` in sqlite) after restart. No manual migration needed.
7. **Smoke-test each fixed flow** in the browser (see checklist below).

## What this branch changes (commit by commit)

| Commit theme | Files/areas | Notes for reconciliation |
|---|---|---|
| git hygiene | `.gitignore`, `.gitattributes` | Enforces LF; untracks `.pyc`/`tests/tmp`. Safe. |
| caution fee + Resident T&C | `app.py` models/routes | Was uncommitted locally; VM likely already has this deployed. **Verify before applying.** |
| escape XSS sinks | `escapeHtml` + 136 sinks | **VM likely already has an escapeHtml fix (July 26). Use the VM's; skip this if so.** |
| manager `updateInquiry` | ROLE_DASHBOARD_TEMPLATE | Defines the missing function. Check VM's unified-pipeline version first. |
| destructive-delete integrity | property/tenant/payment/expense/account routes | Property delete 409-guards; tenant hard-delete detaches financial rows; payment/expense hard-delete CEO-only; "last CEO" protection. Likely new — apply. |
| realtor commission + stats leak + lead delete | `/admin/api/my-commission` (new), `/admin/api/stats`, inquiry delete | Likely new — apply. New endpoint. |
| upload gating + size caps | `/admin/api/upload`, receipt upload | Apply if VM's upload isn't already gated. |
| login audit + lockout | `LoginAudit` model, `/admin/login` | New table + logic. Likely new — apply. |
| investor honest terms + View modals | ROLE + CEO templates | Removes fabricated 3.5%/4yr; real View modals. Likely new — apply. |
| `.0$` regex escape tidy | fmtCompact | Cosmetic; harmless. |

## Post-deploy smoke-test checklist

- [ ] Public contact/inquiry with `<img src=x onerror=alert(1)>` in the name → open CEO
      Inquiries/Approvals tab → **no alert** (escaping works).
- [ ] Property delete on a property that has units → clean 409 message, not a 500.
- [ ] Tenant hard-delete → tenant gone, its payments still listed (tenant_id detached).
- [ ] Manager changes an inquiry status → saves (no `updateInquiry` ReferenceError).
- [ ] Realtor dashboard commission = payroll figure (÷11 on serviced leases), not ×10%.
- [ ] Log in as realtor, `curl /admin/api/stats` → no revenue/capital fields present.
- [ ] Manager/Accountant cannot delete a payment (403); CEO can.
- [ ] 6 wrong passwords for a username → 6th is locked out (429); `login_audit` has rows.
- [ ] New investor with no term set → dashboard shows "TBC"/"To be confirmed", no fake 4yr.
- [ ] CEO Inquiries/Messages "View" opens a modal with the full message body.

## Known follow-ups NOT done in this branch (need infra or larger change)

- **Rate limiter is still `memory://`** — per-worker and resets on restart. Move flask-limiter
  to a shared Redis store for correct multi-worker login/signup limits.
- **No `ProxyFix`** — client IPs behind nginx are the proxy IP. Add
  `werkzeug.middleware.proxy_fix.ProxyFix` so rate limiting and `login_audit.ip` are accurate.
- **No Content-Security-Policy header** — output-encoding is in place, but a CSP would add
  defense-in-depth. Needs browser testing (inline scripts + Tailwind CDN) before shipping.
- **PA role** — this branch does NOT rebuild the PA Assistant Desk; the VM already has it.
  Reconcile from the VM, don't recreate here.
- **Realtor sale commission** — the payroll model only pays rental commission on serviced
  leases; the contract mentions sale commission but nothing computes/pays it. Product decision.
- **JS-in-Python escape warnings** — remaining `\w`, `\/`, `\d`, `\s` (and a latent `\b` that
  Python turns into a backspace) inside the giant template strings. Proper fix is to move the
  dashboard JS to static `.js` files (or raw strings), not blanket-escape. Cosmetic today.
- **Magic-byte upload validation** — uploads are extension-checked only; add content sniffing.
