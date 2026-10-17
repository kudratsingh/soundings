// Radar-scope geometry for WO-084's projected track (WO-086): the 60 s velocity leader from
// the Kalman-smoothed velocity and the 95% projection ellipse, as Leaflet lat/lon points.
// A stress-test estimate: the projection assumes the contact holds its velocity.

/** Metres per degree of latitude (spherical; display only). */
const M_PER_DEG = 111_320

/** The point `s` seconds ahead at east/north velocity (`ve`, `vn`) m/s. */
export function leaderEnd(lat: number, lon: number, ve: number, vn: number, s = 60): [number, number] {
  const cos = Math.max(Math.cos((lat * Math.PI) / 180), 1e-6)
  return [lat + (vn * s) / M_PER_DEG, lon + (ve * s) / (M_PER_DEG * cos)]
}

/** The ellipse outline: semi-axes in metres, major axis at compass bearing `angle_deg`
 * (clockwise from north), `n` points, closed by Leaflet's polygon. */
export function ellipseLatLngs(center: [number, number], a_m: number, b_m: number, angle_deg: number, n = 36): [number, number][] {
  const [lat, lon] = center
  const cos = Math.max(Math.cos((lat * Math.PI) / 180), 1e-6)
  const o = (angle_deg * Math.PI) / 180
  // Unit vectors (east, north) along the major and minor axes.
  const major: [number, number] = [Math.sin(o), Math.cos(o)]
  const minor: [number, number] = [Math.cos(o), -Math.sin(o)]
  const out: [number, number][] = []
  for (let i = 0; i < n; i++) {
    const t = (2 * Math.PI * i) / n
    const e = a_m * Math.cos(t) * major[0] + b_m * Math.sin(t) * minor[0]
    const nn = a_m * Math.cos(t) * major[1] + b_m * Math.sin(t) * minor[1]
    out.push([lat + nn / M_PER_DEG, lon + e / (M_PER_DEG * cos)])
  }
  return out
}
