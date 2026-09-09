# Website Design Brief — Employee Identity-Aware Workstation Monitoring Platform

*Adapted from a tech-review-channel template. Structure mirrors the original;
content, palette, and vibe are rebuilt for an enterprise workforce-analytics
product where trust and clarity matter more than charisma.*

---

## Visual Strategy

**Imagery**: abstracted dashboard visualizations, stylized camera/sensor
iconography, clean product photography of hardware (camera units, desk
setups) — **never** real photos of identifiable people being monitored.
Case-study imagery uses blurred silhouettes or illustrated figures, not
recognizable employees. This is a deliberate trust decision, not a
stock-photo shortcut: a site selling workplace monitoring software that
shows real, identifiable faces on desks undercuts the "this is respectful,
not surveillance" message before a visitor reads a word of copy.

**Photography**: clean, even, cool-toned lighting — closer to a modern
security-operations-center or data-center aesthetic than dramatic
high-contrast noir. Precision over drama.

**Composition**: dashboard-first hero blocks, 16:9, showing the product's
actual interface (occupancy states, identity match indicators) rather than
lifestyle photography.

## Color Palette

**Primary Colors**: deep navy (`#0F2138`), signal blue *(pull exact value
from your logo — placeholder: `#2D6CDF`)*, pure white (`#FFFFFF`).

**Accent Colors**: electric cyan (`#3FD2FF`) for "live/active" states, warm
amber (`#F5A623`) for attention states.

Deliberate substitution from the original brief: the reference used **red**
as a primary color, which reads naturally as "hype" for a tech-review
channel. For this product, red on a monitoring dashboard reads as *alarm* —
and this system is explicitly not an alarm system (Section 2.2 of the
project documentation: informational monitoring, not automated
discipline). Amber replaces red everywhere a lesser-attention accent is
needed, reserving true red only for genuine system errors, never for normal
product states like MISMATCH or UNKNOWN.

**Background**: deep navy, not pure black — pure black plus a bright accent
is one of the most common "AI-generated dashboard" tells, and it also
reads closer to noir/surveillance than confident enterprise software. A
very subtle grid or scanline texture (evoking sensor/data precision)
replaces the original's film-grain overlay (which evokes cinema, not data).

## Typography

**Headings**: a confident, architectural geometric sans with real weight —
heavy but engineered-feeling, not shouty. Think structural confidence, not
entertainment-channel loudness.

**Body Text**: clean grotesque, same instinct as the original — this
choice transfers directly regardless of domain.

**Layout**: dashboard-first, with collapsible technical/compliance depth
underneath — the direct translation of "video-first with collapsible
written companion." A visitor scans the live-feeling demo first, then can
expand into architecture detail, compliance documentation, or a case
study without leaving the page.

## Page Structure

1. **Hero**: an animated, autoplay-on-hover preview of the live dashboard —
   stylized desk icons cycling through VACANT → scanning → MATCH, not real
   camera footage.
2. **Solution Library**: filterable by industry vertical (BPO/Call Centers,
   Manufacturing, Warehousing, Hot-Desking Offices, BFSI Compliance) —
   direct translation of the chronological video library with category
   filters.
3. **Case Studies & Technical Deep-Dives**: longer-form companions to each
   solution — architecture notes, accuracy methodology, compliance detail
   — replacing "written reviews."
4. **Pricing Calculator**: "What's included at X cameras / X employees" —
   a direct, honestly better fit than the original's product-recommendation
   framing, since this *is* a configurator, not a buying guide.
5. **Newsletter & Book a Demo** — replaces Newsletter & Patreon. A demo
   booking is the equivalent conversion action for enterprise software;
   "support the creator" has no equivalent here, so it isn't force-fit.

## Interaction Details

- Solution-library cards play a short **silent looping animation** on
  hover — a desk icon cycling through occupancy/identity states with a
  subtle blue scan-line sweep. No real footage, ever.
- Industry-vertical filters slide cards into a new arrangement with the
  same 300ms shuffle as the original — this pattern transfers untouched.
- Case studies use a **sticky animated dashboard replay** pinned beside the
  text as you scroll — the direct equivalent of "read along while video
  plays."
- The pricing calculator uses a **camera-count / employee-count slider**
  that re-sorts and recalculates the recommended plan in real time — this
  maps onto the original's price-range slider almost exactly, just with
  the input and output swapped to fit a configurator rather than a
  shopping guide.
- Newsletter signup uses a typewriter-style placeholder cycling through
  real value props: *"See how a 500-seat BPO cut shrinkage 22%..."*,
  *"Get monthly workforce-analytics benchmarks..."*, *"New: activity
  detection now in beta..."*
- Scroll-driven color shift: the page begins in deep navy (the technology)
  and gradually warms/lightens as sections move from capability into
  compliance and human-centered practice — a deliberate narrative arc, not
  decoration: impressive tech, then "here's how much we respect your
  people."

## Overall Vibe

**Precise, trustworthy, quietly confident, modern.** Not "opinionated and
charismatic" — that voice suits a personality-driven channel where the
host's taste *is* the product. Here, the buyer is trusting this system with
sensitive biometric data; the site's job is to earn that trust through
clarity and restraint, not to perform confidence through volume.
