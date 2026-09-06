WAQI WEBSITE V19.29 — RESPONSIVE PERFORMANCE + MICRO POLISH

Base: V19.28 TARGETED VISUAL FIX

Performance changes:
- Desktop keeps the original 4K hero/scenic assets.
- Phones now receive 1280–1400px high-quality WebP variants instead of 4K panoramas.
- Tablets now receive 2048–2304px high-quality WebP variants instead of 4K panoramas.
- Device-specific preload rules load only the matching hero/scenic/comic assets.
- Comic images use responsive srcset and low fetch priority while still beginning early.
- Header/favicon Waqi symbol uses an exact-aspect, uncropped optimized copy (original remains untouched in assets).
- Homepage consultation and partner imagery use responsive WebP sources.
- No scroll reveal / delayed content rendering was reintroduced.

Visual micro-polish:
- Balanced headline wrapping where browser-supported.
- Subtle desktop depth on major editorial imagery only.
- Existing layouts, copy, forms, FAQ styling, scroll cue positions and page backgrounds are otherwise preserved.

Validation performed:
- CSS brace balance checked.
- JavaScript syntax checked with node --check.
- Local asset references checked (sms: links and CSS custom-property URL semantics excluded from filesystem check).
- Responsive image dimensions and generated assets checked.
- ZIP integrity checked after packaging.
- Chromium screenshot attempt remains unavailable in this runtime because headless Chromium times out; no browser pixel-test claim is made.
