# Changelog: item 12 -- frontend design pass (removing the generic-template look)

Base commit expected: `dfd72aa`, or the repo-hygiene commit on top of it.
If `git log --oneline -3` shows neither, an earlier zip was skipped -- stop
and tell Claude.

Frontend only. No backend, model, database or test files are touched.

## What this app is, and how the brief was applied

The brief is written for public marketing sites (hero, testimonials, SEO,
pricing). This is a sign-in-only admin tool for people who watch desks and
attendance, so the parts that fit were applied and the rest were left out
on purpose:

- Applied: a distinct visual direction, real design tokens, specific
  plain-language copy, no decorative gradients/glass/blobs/emoji, meaningful
  status shapes, accessibility, mobile as its own layout, performance.
- Not applied: hero, testimonials, social proof, pricing, blog, structured
  data. There is nothing to sell or index here. The pages carry
  `<meta name="robots" content="noindex">` because an admin tool should not
  be crawled.
- Nothing was invented: no statistics, quotes, names or logos. The one
  illustrative element (three sample desks on the sign-in page) is labelled
  as made-up sample data.

## Direction: an occupancy board

The subject is desks, rooms and time, so the interface reads like a
timetable rather than a SaaS dashboard: ruled rows, left-aligned, dense,
one strong element (the desk board) and everything else quiet.

| Decision | What | Why |
|---|---|---|
| Header | Removed the indigo gradient bar. Plain white bar, logo on its own colour | The gradient header was the most template-like element. The logo is blue-on-white, so it now sits on white as designed instead of inside a padded white box |
| Colour | Brand blue `#2A29A3` (from the logo) only for actions, the active section and "at desk". Status colours are for status only. Neutral greys otherwise | Every colour now has one job. The old gradients and the `--indigo/--navy` tokens are gone |
| Type | Barlow (text) + Barlow Semi Condensed (headings, desk names, numbers). Self-hosted woff2, 7 files, about 160 KB | Signage/timetable letterforms fit a desk board. Self-hosted so it works offline on Lightning and sends nothing to third parties |
| Corners | Panels, tables, board and preview are square. Controls and tags use 2px. No pills | A radius hierarchy instead of one radius on everything |
| Panels | A heavy top rule, a heading, content. Not boxed cards | Boxes are kept only for a contained thing (the duplicate-video notice) |
| Navigation | Left section list grouped by task: Monitor / Set up / Try it on footage. Tab names unchanged | Groups the daily views apart from setup and demo tools. Collapses to a scrolling strip on mobile |
| Desk board | Ruled cells, not floating cards. A mismatch is tinted, a vacant desk is recessed | Loudness follows urgency |
| Status shapes | filled circle = match/present, triangle = mismatch, hollow circle = unknown, dash = vacant/signed out, square = on break | Status no longer depends on colour alone |
| Timeline | Blue = at desk, ochre = in room. Gaps are hatched | Hatching means "nothing recorded", not zero |
| Motion | None on load. Hover/focus colour changes and the progress bar only. Reduced-motion respected | |
| Sign-in page | Two columns: what the system does + sample board, and the form | Replaces the centred 340px card |

## Copy

Rewrote every hint, heading and button in plain sentences. Removed developer
references from user-facing text (file paths, "Section 5.2 of the project
documentation", "great for showing a prospect"). Action names are now
consistent: "Run analysis" (was "Run AI Analysis"), "Use this video" (was
"Select this video"). Tile text no longer uses em dashes or middle dots.
Wording that states behaviour (identification on vacant-to-occupied, then
every 60 s, or 5 s while UNKNOWN) was checked against `stream_worker.py`.

## Real bugs fixed along the way

- `showTab()` marked the active section from `event.target`. When
  `viewRunInAttendance()` (the "View" button in Past analysis runs) called it,
  the table's View button became "active" and every section button lost its
  highlight. It now uses each button's `data-tab`. Verified in a browser.
- Stats rows in Attendance were mouse-only. They now take focus and respond
  to Enter.
- "Who's here now" showed ON_BREAK people in the same green as PRESENT.
  ON_BREAK now has its own amber bar.
- Every `<label>` was unassociated with its input. All now use `for=`, and the
  sign-in fields are in a real `<form>` (Enter submits, password managers work).
- Message areas announce changes to screen readers (`aria-live`).

## What did not change

Every element id and every `onclick` handler in `dashboard.html` is
preserved (checked by script: 0 missing ids, 0 dropped handlers), so all tab
JS works unmodified apart from the small edits listed below.

JS edits (small): `dashboard.page.js` (showTab), `tabs/dashboard.js` and
`tabs/attendance.js` (tile text, ON_BREAK, row keyboard), `tabs/workstations.js`
(ROI canvas colours match the new tokens), `tabs/videoanalysis.js` (two inline
styles became classes).

## Verification done before shipping

Rendered in headless Chromium with a mocked backend, at 1280 px and 390 px:

- Zero page errors. All seven sections and the sign-in page rendered and
  reviewed as screenshots.
- No horizontal page scroll at 390 px on sign-in, dashboard or attendance.
- Fonts load (Barlow 400/500/600, Barlow Semi Condensed 500/600).
- Contrast: every text/background pair is at least 5.7:1.
- No stale colour tokens, no leftover gradients other than the hatch pattern
  and the sidebar column.
- `node --check` passes on every JS file.

Not verified: real backend data (the mock covered the shapes the JS reads),
Safari/Firefox, and a screen reader. Frontend has no test framework.

## Apply, then check on Lightning

    git pull
    bash run_frontend.sh        # and bash run_backend.sh in the other terminal

Hard-refresh the browser (Ctrl+Shift+R) so the old CSS is not cached, then:

1. Sign-in page shows two columns; sign in works, and pressing Enter works.
2. Dashboard: desk board tiles; a MISMATCH desk is tinted; vacant is grey.
3. Video Analysis > Past analysis runs > View: lands on Attendance with
   only "Attendance" highlighted in the left list.
4. Attendance: load a timeline; gaps are hatched.
5. Resize the window narrow (or open on a phone): the section list becomes a
   strip under the header.

## Log to send Claude

Frontend has no test log. Send screenshots of the sign-in page, Dashboard,
Attendance (with a timeline loaded) and one phone-width view, plus anything
in the browser console (F12 > Console) that shows a red error. Also send
`git log --oneline -3`.
