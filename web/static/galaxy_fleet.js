/* Small-scale cinematic fleet silhouettes for the Galaxy canvas player. */
(function (root) {
  'use strict';

  function rgb(hex) {
    const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
    return match ? match.slice(1).map(part => parseInt(part, 16)) : [220, 225, 238];
  }

  function halo(g, color, glow) {
    if (glow <= 0) return;
    const value = rgb(color), radius = 15 + glow * 12;
    const gradient = g.createRadialGradient(0, 0, 0, 0, 0, radius);
    gradient.addColorStop(0, `rgba(${value.join(',')},${(.45 * glow).toFixed(3)})`);
    gradient.addColorStop(1, `rgba(${value.join(',')},0)`);
    g.fillStyle = gradient;
    g.fillRect(-radius, -radius, radius * 2, radius * 2);
  }

  function explorer(g, color, now, thrust) {
    const pulse = .72 + .28 * Math.sin(now / 110);
    g.fillStyle = color; g.strokeStyle = '#252936'; g.lineWidth = .65;
    g.beginPath(); g.ellipse(7, 0, 10.5, 6.1, 0, 0, Math.PI * 2); g.fill(); g.stroke();
    g.fillStyle = '#b9c7dd';
    g.beginPath(); g.ellipse(8.5, -.8, 6.5, 2.5, 0, Math.PI, Math.PI * 2); g.fill();
    g.fillStyle = color;
    g.beginPath(); g.moveTo(0,-2.4); g.lineTo(-7,-4.2); g.lineTo(-10,-1.8); g.lineTo(-2,1.8); g.closePath(); g.fill(); g.stroke();
    for (const side of [-1, 1]) {
      g.fillStyle = '#aeb8ca';
      g.beginPath(); g.roundRect(-10, side * 7 - 1.25, 13, 2.5, 1.2); g.fill(); g.stroke();
      g.fillStyle = thrust ? `rgba(90,190,255,${pulse.toFixed(3)})` : '#475064';
      g.beginPath(); g.arc(-10.2, side * 7, 1.35, 0, Math.PI * 2); g.fill();
      g.strokeStyle = 'rgba(100,190,255,.7)'; g.lineWidth = .45;
      g.beginPath(); g.moveTo(-7, side * 7); g.lineTo(1, side * 7); g.stroke();
    }
    g.fillStyle = '#72b9ed';
    g.beginPath(); g.ellipse(12, 0, 2.4, 1.4, 0, 0, Math.PI * 2); g.fill();
  }

  function destroyer(g, color, now, thrust) {
    const value = rgb(color), pulse = .65 + .35 * Math.sin(now / 85);
    g.fillStyle = color; g.strokeStyle = '#262630'; g.lineWidth = .7;
    g.beginPath(); g.moveTo(18,0); g.lineTo(-13,-10); g.lineTo(-9,0); g.lineTo(-13,10); g.closePath(); g.fill(); g.stroke();
    g.fillStyle = `rgba(${value.join(',')},.38)`;
    g.beginPath(); g.moveTo(15,0); g.lineTo(-10,-7); g.lineTo(-6,0); g.closePath(); g.fill();
    g.strokeStyle = 'rgba(30,32,40,.65)'; g.lineWidth = .45;
    for (let x = -7; x <= 8; x += 4) {
      g.beginPath(); g.moveTo(x, -Math.max(1, (11-x) * .34)); g.lineTo(x, Math.max(1, (11-x) * .34)); g.stroke();
    }
    g.fillStyle = '#6d707b'; g.fillRect(-5,-3.4,7,6.8);
    g.fillStyle = '#858995'; g.fillRect(-3,-5.7,4.5,2.6);
    g.fillStyle = '#a8adba'; g.fillRect(-1.8,-8,1.2,2.5);
    if (thrust) {
      g.fillStyle = `rgba(115,170,255,${pulse.toFixed(3)})`;
      for (const y of [-5, 0, 5]) { g.beginPath(); g.arc(-12.2,y,1.25,0,Math.PI*2); g.fill(); }
    }
  }

  function battlestar(g, color, now, thrust) {
    const pulse = .65 + .35 * Math.sin(now / 95);
    g.fillStyle = color; g.strokeStyle = '#292a31'; g.lineWidth = .7;
    g.beginPath(); g.moveTo(17,0); g.lineTo(9,-4); g.lineTo(-10,-4.8); g.lineTo(-15,-2); g.lineTo(-15,2); g.lineTo(-10,4.8); g.lineTo(9,4); g.closePath(); g.fill(); g.stroke();
    for (const side of [-1, 1]) {
      g.fillStyle = '#85858b';
      g.beginPath(); g.roundRect(-9,side*9-2.2,18,4.4,1.3); g.fill(); g.stroke();
      g.fillStyle = '#454750'; g.fillRect(-6.5,side*9-1.2,13,2.4);
      g.strokeStyle = 'rgba(210,215,225,.35)'; g.lineWidth = .4;
      for (let x=-5;x<=5;x+=2.5){g.beginPath();g.moveTo(x,side*9-1);g.lineTo(x,side*9+1);g.stroke()}
    }
    g.strokeStyle = 'rgba(40,42,49,.7)'; g.lineWidth = .5;
    for (let x=-9;x<=10;x+=3){g.beginPath();g.moveTo(x,-3.5);g.lineTo(x,3.5);g.stroke()}
    g.fillStyle = '#9b9da5'; g.fillRect(-1,-6,5,3); g.fillRect(-1,3,5,3);
    if (thrust) {
      g.fillStyle = `rgba(100,180,255,${pulse.toFixed(3)})`;
      for (const y of [-2.4,0,2.4]) { g.beginPath(); g.arc(-15,y,.9,0,Math.PI*2); g.fill(); }
    }
  }

  function galaxyFleetDraw(g, effect, position, scale, angle, now, glow, color, thrust) {
    g.save(); g.translate(position[0], position[1]); g.rotate(angle); g.scale(scale, scale);
    halo(g, color, glow);
    g.shadowColor = color; g.shadowBlur = 5;
    if (effect === 'explorer') explorer(g, color, now, thrust);
    else if (effect === 'destroyer') destroyer(g, color, now, thrust);
    else if (effect === 'battlestar') battlestar(g, color, now, thrust);
    g.restore();
  }

  root.galaxyFleetDraw = galaxyFleetDraw;
})(window);
