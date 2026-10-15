/* Pure timing helpers shared by the UI and Node regression tests. */
((root) => {
  "use strict";
  function previous(times, time) {
    let lo = 0, hi = times.length;
    while (lo < hi) { const mid = (lo + hi) >>> 1; if (times[mid] <= time + 1e-9) lo = mid + 1; else hi = mid; }
    return lo - 1;
  }
  function frameAt(video, time) {
    if (!video || time < video.offset || time > video.end + 1e-6) return {index:-1, outside:true};
    const index = previous(video.times, time);
    return {index, outside:index < 0, age:index < 0 ? null : time - video.times[index]};
  }
  function step(video, time, direction, duration) {
    if (!video?.times.length) return Math.max(0, Math.min(duration, time + direction * .02));
    let index = previous(video.times, time);
    index = direction > 0 ? index + 1 : (index >= 0 && Math.abs(video.times[index] - time) < 1e-6 ? index - 1 : index);
    return video.times[Math.max(0, Math.min(video.times.length - 1, index))];
  }
  function paths(times, values, column, gap, x, y, breaks=null) {
    let path = "", last = null;
    times.forEach((time, i) => {
      const value = values[i][column];
      if (value === null || !Number.isFinite(value)) { last = null; return; }
      path += `${last === null || (breaks ? breaks[i] : time - last > gap) ? "M" : "L"}${x(time).toFixed(2)},${y(value).toFixed(2)} `;
      last = time;
    });
    return path;
  }
  const api = {previous, frameAt, step, paths};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.RawTimeline = api;
})(globalThis);
