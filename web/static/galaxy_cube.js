/* Klangtresor music-matrix cube, adapted for the Galaxy canvas player. */
(function (root) {
  'use strict';

  function hexRgb(value) {
    const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(value || '');
    return match ? match.slice(1).map(part => parseInt(part, 16)) : [164, 236, 108];
  }

  function mix(a, b, amount) {
    return Math.round(a + (b - a) * amount);
  }

  function galaxyCubeDraw(g, position, scale, now, glow, color) {
    const half = 7.2;
    const eyeDistance = 50;
    const accent = hexRgb(color);
    const yaw = 0.66 + now / 40000 * Math.PI * 2;
    const pitch = 0.38;
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    const cx = Math.cos(pitch), sx = Math.sin(pitch);
    const source = [
      [-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1],
      [-1,-1, 1],[1,-1, 1],[1,1, 1],[-1,1, 1]
    ];
    const corners = source.map(([ex, ey, ez]) => {
      const x = ex * half, y = ey * half, z = ez * half;
      const x1 = x * cy + z * sy;
      const z1 = -x * sy + z * cy;
      const y2 = y * cx - z1 * sx;
      const z2 = y * sx + z1 * cx;
      const perspective = eyeDistance / (eyeDistance - z2);
      return [x1 * perspective, y2 * perspective, z2];
    });
    const faces = [[0,1,2,3],[4,7,6,5],[0,4,5,1],[3,2,6,7],[0,3,7,4],[1,5,6,2]]
      .map(face => ({face, depth: face.reduce((sum, index) => sum + corners[index][2], 0) / 4}))
      .sort((a, b) => a.depth - b.depth);
    const point = (a, b, amount) => [
      corners[a][0] + (corners[b][0] - corners[a][0]) * amount,
      corners[a][1] + (corners[b][1] - corners[a][1]) * amount
    ];

    g.save();
    g.translate(position[0], position[1]);
    g.scale(scale, scale);
    if (glow > 0) {
      const radius = 11 + glow * 11;
      const halo = g.createRadialGradient(0, 0, 0, 0, 0, radius);
      halo.addColorStop(0, `rgba(${accent.join(',')},${(0.55 * glow).toFixed(3)})`);
      halo.addColorStop(1, `rgba(${accent.join(',')},0)`);
      g.fillStyle = halo;
      g.fillRect(-radius, -radius, radius * 2, radius * 2);
    }
    for (const {face, depth} of faces) {
      const light = Math.max(0, Math.min(1, 0.5 + depth / (half * 2)));
      g.beginPath();
      g.moveTo(corners[face[0]][0], corners[face[0]][1]);
      for (let index = 1; index < face.length; index++) {
        g.lineTo(corners[face[index]][0], corners[face[index]][1]);
      }
      g.closePath();
      g.fillStyle = `rgb(${mix(8, accent[0] * .24, light)},${mix(13, accent[1] * .25, light)},${mix(11, accent[2] * .24, light)})`;
      g.fill();
      g.strokeStyle = `rgba(${accent.join(',')},${(0.28 + light * .5).toFixed(3)})`;
      g.lineWidth = 0.55;
      g.stroke();
      if (depth < 0) continue;
      g.save();
      g.clip();
      g.strokeStyle = `rgba(${accent.join(',')},${(0.13 + light * .18).toFixed(3)})`;
      g.lineWidth = 0.28;
      for (let index = 1; index < 4; index++) {
        const amount = index / 4;
        let a = point(face[0], face[1], amount), b = point(face[3], face[2], amount);
        g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke();
        a = point(face[0], face[3], amount); b = point(face[1], face[2], amount);
        g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke();
      }
      g.restore();
    }
    const front = faces[faces.length - 1].face;
    const eyeX = front.reduce((sum, index) => sum + corners[index][0], 0) / 4;
    const eyeY = front.reduce((sum, index) => sum + corners[index][1], 0) / 4;
    const pulse = 0.45 + 0.55 * Math.pow(0.5 + 0.5 * Math.sin(now / 620), 2);
    g.globalCompositeOperation = 'lighter';
    const eye = g.createRadialGradient(eyeX, eyeY, 0, eyeX, eyeY, 4);
    eye.addColorStop(0, `rgba(235,255,235,${pulse.toFixed(3)})`);
    eye.addColorStop(0.35, `rgba(${accent.join(',')},${(pulse * .7).toFixed(3)})`);
    eye.addColorStop(1, `rgba(${accent.join(',')},0)`);
    g.fillStyle = eye;
    g.fillRect(eyeX - 4, eyeY - 4, 8, 8);
    g.restore();
  }

  root.galaxyCubeDraw = galaxyCubeDraw;
})(window);
