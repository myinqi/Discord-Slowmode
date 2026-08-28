/* Shared Suno playback loader for encrypted media_urls assets. */
(function (root) {
  'use strict';

  const states = new WeakMap();

  function stateFor(audio) {
    let state = states.get(audio);
    if (!state) {
      state = { generation: 0, objectUrl: null };
      states.set(audio, state);
    }
    return state;
  }

  function revoke(state) {
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = null;
  }

  function fromBase64(value) {
    return Uint8Array.from(atob(value), character => character.charCodeAt(0));
  }

  async function decrypt(uuid, encryptedUrl, license, maxBytes) {
    if (!root.crypto || !root.crypto.subtle) throw new Error('WebCrypto unavailable');
    const context = new TextEncoder().encode(uuid);
    const digest = await root.crypto.subtle.digest('SHA-256', new TextEncoder().encode(license.glt));
    const guestKey = await root.crypto.subtle.importKey('raw', digest, { name: 'AES-GCM' }, false, ['decrypt']);
    async function unwrap(value, asKey) {
      const wrapped = fromBase64(value);
      const raw = await root.crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: wrapped.slice(0, 12), additionalData: context },
        guestKey,
        wrapped.slice(12)
      );
      return asKey
        ? root.crypto.subtle.importKey('raw', raw, { name: 'AES-CTR' }, false, ['decrypt'])
        : new Uint8Array(raw);
    }
    const [contentKey, contentIv, response] = await Promise.all([
      unwrap(license.key, true),
      unwrap(license.iv, false),
      fetch(encryptedUrl),
    ]);
    if (!response.ok) throw new Error(`Suno audio HTTP ${response.status}`);
    const contentLength = Number(response.headers.get('content-length') || 0);
    if (contentLength > maxBytes) throw new Error('Suno audio asset is too large');
    const encrypted = await response.arrayBuffer();
    if (encrypted.byteLength > maxBytes) throw new Error('Suno audio asset is too large');
    const clear = await root.crypto.subtle.decrypt(
      { name: 'AES-CTR', counter: contentIv, length: 128 },
      contentKey,
      encrypted
    );
    const bytes = new Uint8Array(clear);
    if (bytes.length < 12 || String.fromCharCode(...bytes.slice(4, 8)) !== 'ftyp') {
      throw new Error('Invalid decrypted Suno audio');
    }
    return URL.createObjectURL(new Blob([clear], { type: 'audio/mp4' }));
  }

  async function load(audio, uuid, options) {
    options = options || {};
    const state = stateFor(audio), generation = ++state.generation;
    const status = typeof options.onStatus === 'function' ? options.onStatus : function () {};
    const apiBase = String(options.apiBase || '').replace(/\/$/, '');
    const maxBytes = Number(options.maxBytes || 64 * 1024 * 1024);
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    revoke(state);
    status('Resolving Suno audio…');
    const response = await fetch(`${apiBase}/api/suno-playback/${encodeURIComponent(uuid)}`);
    const descriptor = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(descriptor.error || `Playback HTTP ${response.status}`);
    let source = descriptor.audio_url, isObjectUrl = false;
    if (descriptor.encrypted) {
      status('Decrypting Suno audio…');
      source = await decrypt(uuid, descriptor.audio_url, descriptor.license || {}, maxBytes);
      isObjectUrl = true;
    }
    if (generation !== state.generation) {
      if (isObjectUrl) URL.revokeObjectURL(source);
      return false;
    }
    if (isObjectUrl) state.objectUrl = source;
    audio.src = source;
    audio.load();
    status('');
    if (options.autoplay !== false) {
      try {
        await audio.play();
      } catch (error) {
        // Decryption can outlive the browser's transient click permission.
        // The source is ready, so a second press can start it immediately.
        if (error && error.name === 'NotAllowedError') status('Press ▶ to play');
        else throw error;
      }
    }
    return true;
  }

  function release(audio) {
    const state = stateFor(audio);
    state.generation++;
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    revoke(state);
  }

  root.SunoPlayback = { load, release };
})(window);
