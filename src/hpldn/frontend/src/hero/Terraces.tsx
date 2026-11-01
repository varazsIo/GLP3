import React from 'react';

/** One house's geometry in SVG units. */
interface House {
  x: number;
  width: number;
  roof: number;
  eaves: number;
  windows: Array<{ x: number; y: number; lit: boolean }>;
  chimney: number;
}

/**
 * Returns a deterministic pseudo-random generator, so the skyline is the same on every render.
 */
function seeded(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Builds a row of houses across the viewBox.
 *
 * @param seed Seed for the house sizes and lit windows.
 * @param baseY Ground line of the row.
 * @param scale Size multiplier, smaller for the back row.
 */
function buildRow(seed: number, baseY: number, scale: number): House[] {
  const random = seeded(seed);
  const houses: House[] = [];
  let x = -40;
  while (x < 1480) {
    const width = (62 + random() * 34) * scale;
    const height = (150 + random() * 70) * scale;
    const roof = baseY - height;
    const eaves = roof + 22 * scale;
    const windows: Array<{ x: number; y: number; lit: boolean }> = [];
    const cols = width > 80 * scale ? 2 : 1;
    const rows = Math.max(2, Math.round((baseY - eaves) / (46 * scale)));
    for (let c = 0; c < cols; c += 1) {
      for (let r = 0; r < rows; r += 1) {
        windows.push({
          x: x + (width / (cols + 1)) * (c + 1) - 9 * scale,
          y: eaves + 18 * scale + r * 42 * scale,
          lit: random() > 0.68,
        });
      }
    }
    houses.push({ x, width, roof, eaves, windows, chimney: x + width * (0.12 + random() * 0.1) });
    x += width + 2 * scale;
  }
  return houses;
}

const BACK_ROW = buildRow(1337, 470, 0.78);
const FRONT_ROW = buildRow(90210, 560, 1);

/**
 * Draws one row of houses with chimneys, roofs and windows.
 */
function Row({
  houses,
  fill,
  windowFill,
  litFill,
  scale,
}: {
  houses: House[];
  fill: string;
  windowFill: string;
  litFill: string;
  scale: number;
}): React.ReactElement {
  return (
    <g>
      {houses.map((house) => (
        <g key={fill + '-' + house.x}>
          <rect
            x={house.chimney}
            y={house.roof - 26 * scale}
            width={14 * scale}
            height={30 * scale}
            fill={fill}
          />
          <path
            d={
              'M' +
              (house.x - 3) +
              ' ' +
              house.eaves +
              'L' +
              (house.x + house.width / 2) +
              ' ' +
              house.roof +
              'L' +
              (house.x + house.width + 3) +
              ' ' +
              house.eaves +
              'Z'
            }
            fill={fill}
          />
          <rect x={house.x} y={house.eaves} width={house.width} height={600 - house.eaves} fill={fill} />
          {house.windows.map((win) => (
            <rect
              key={win.x + '-' + win.y}
              x={win.x}
              y={win.y}
              width={18 * scale}
              height={26 * scale}
              rx={2}
              fill={win.lit ? litFill : windowFill}
            />
          ))}
        </g>
      ))}
    </g>
  );
}

/**
 * A stylised row of London terraces behind the hero.
 *
 * Drawn rather than photographed, so the page loads nothing over the network,
 * and dark so white type sits cleanly on the scrim above it.
 */
export function Terraces({ className }: { className?: string }): React.ReactElement {
  return (
    <svg
      className={className}
      viewBox="0 0 1440 600"
      preserveAspectRatio="xMidYMax slice"
      aria-hidden="true"
      focusable="false"
    >
      <Row houses={BACK_ROW} fill="#132a22" windowFill="#0d1f19" litFill="#27453a" scale={0.78} />
      <Row houses={FRONT_ROW} fill="#0a1713" windowFill="#0f241d" litFill="#c9a227" scale={1} />
    </svg>
  );
}

export default Terraces;
