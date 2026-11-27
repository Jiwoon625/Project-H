// Derive metrics from the current snapshot; a missing measurement stays null.
function assessment(home, access) {
  const factors = home.sb;
  const share = value => Math.max(0, Math.min(1, value));
  const times = (access?.times || []).filter(Number.isFinite).sort((a,b) => a-b);
  const trips = times.length >= 2
    ? 24 * share((85 - (times[0] + times[1]) / 2) / 65)
      + 12 * times.filter(t => t <= 45).length / ACCESS.hubs.length : null;
  const rail = 14 * (factors[0] + factors[1] + factors[2]) / 30;
  const schoolRows = access?.schools || [];
  const walkable = schoolRows.filter(row => row[1] != null);
  // Show every linked school; score the longest option, including joint catchments.
  const school = schoolRows.length && walkable.length === schoolRows.length
    ? 18 * share((1800 - Math.max(...walkable.map(row => row[1]))) / 1500) : null;
  const secondary = factors[4] / 2;
  const amenities = 1.4 * (factors[5] + factors[6]);
  const x = trips != null && school != null ? trips + rail + school + secondary + amenities : null;
  return {x, trips, rail, school, secondary, amenities,
    product: 100 * (factors[7] + factors[8] + factors[9]) / 15,
    demand: 100 * (factors[10] + factors[11]) / 15};
}

function distanceMeters(a, b) {
  const radians = Math.PI / 180;
  return Math.hypot((a.lat-b.lat)*111195,
    (a.lng-b.lng)*111195*Math.cos((a.lat+b.lat)*radians/2));
}

// 1순위 같은 동(3곳 이상) → 2순위 같은 구(3곳 이상) → 3순위 반경 2km 인접 구·시(참고).
// 3순위는 같은 구에서도 3곳을 못 채운 단지에만 적용하고 화면에서 '참고'로 구분한다.
function peerComparisons(homes, area) {
  const price = 'p'+area, count = 'n'+area;
  const eligible = homes.filter(h => h.stl === 0 && h[price] > 0 && h[count] > 0 && h.y > 0 && h.t &&
    Number.isFinite(h.lat) && Number.isFinite(h.lng));
  const results = new Map();
  for (const h of eligible) {
    const near = eligible.filter(p => p.c !== h.c && p.t === h.t && Math.abs(p.y-h.y) <= 5 &&
      distanceMeters(h,p) <= 2000);
    const local = near.filter(p => h.si === p.si && h.g === p.g && h.d && p.d === h.d);
    const district = near.filter(p => h.si === p.si && h.g === p.g);
    let peers, scope, adjacent = false;
    if (local.length >= 3) { peers = local; scope = '같은 동'; }
    else if (district.length >= 3) { peers = district; scope = '같은 구'; }
    else if (near.length >= 3) { peers = near; scope = '같은 구'; adjacent = true; }
    else { peers = district; scope = '같은 구'; }
    peers = peers.slice().sort((a,b) => a[price]-b[price] || a.c.localeCompare(b.c));
    const n = peers.length;
    const median = n >= 3 ? (peers[(n-1)>>1][price]+peers[n>>1][price])/2 : null;
    const gap = median == null ? null : 100*(h[price]/median-1);
    const trades = peers.reduce((sum,p) => sum+p[count],0);
    const confidence = median == null ? '비교 부족' : h[count] === 1 ? '낮음' :
      (h[count] >= 3 && n >= 5 && trades >= 10 ? '높음' : '보통');
    results.set(h.c,{peers, median, gap, trades, confidence, scope, adjacent,
      crossDistrict: adjacent && peers.some(p => p.si !== h.si || p.g !== h.g)});
  }
  return results;
}
