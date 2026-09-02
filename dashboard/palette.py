"""Colour roles for the dashboard, as CSS custom properties.

Roles, not hexes, are used throughout the page so light and dark swap in one
place. Assignments follow the job each colour does:

* the fitted curve and the two mark types are CATEGORICAL (identity) and take
  the first fixed slots — never cycled, and only three are on screen at once,
  which is the cap for scatter-type forms;
* cheap/rich is POLARITY, so it uses the diverging blue<->red pair with a
  neutral midpoint, never two arbitrary hues;
* the switch verdict is STATUS, from the reserved status palette, and always
  ships with a word beside it so colour never carries the meaning alone.
"""

CSS = """
:root {
  color-scheme: light;
  --surface:        #fcfcfb;
  --plane:          #f9f9f7;
  --ink:            #0b0b0b;
  --ink-2:          #52514e;
  --muted:          #898781;
  --grid:           #e1e0d9;
  --axis:           #c3c2b7;
  --border:         rgba(11,11,11,0.10);
  --series-1:       #2a78d6;   /* fitted curve */
  --series-2:       #eb6834;   /* executed trades */
  --cheap:          #2a78d6;   /* diverging: yields above the curve */
  --rich:           #e34948;   /* diverging: yields below the curve */
  --neutral:        #f0efec;   /* diverging midpoint */
  --good:           #0ca30c;
  --warning:        #fab219;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface:      #1a1a19;
    --plane:        #0d0d0d;
    --ink:          #ffffff;
    --ink-2:        #c3c2b7;
    --muted:        #898781;
    --grid:         #2c2c2a;
    --axis:         #383835;
    --border:       rgba(255,255,255,0.10);
    --series-1:     #3987e5;
    --series-2:     #d95926;
    --cheap:        #3987e5;
    --rich:         #e66767;
    --neutral:      #383835;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface:      #1a1a19;
  --plane:        #0d0d0d;
  --ink:          #ffffff;
  --ink-2:        #c3c2b7;
  --muted:        #898781;
  --grid:         #2c2c2a;
  --axis:         #383835;
  --border:       rgba(255,255,255,0.10);
  --series-1:     #3987e5;
  --series-2:     #d95926;
  --cheap:        #3987e5;
  --rich:         #e66767;
  --neutral:      #383835;
}
"""
