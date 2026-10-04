'use strict';
const film = document.querySelector('#project-film');
const cover = document.querySelector('#film-play');
async function playFilm() {
  cover.hidden = true;
  try { await film.play(); } catch { cover.hidden = false; }
}
cover.addEventListener('click', playFilm);
document.querySelectorAll('[data-watch]').forEach(link => link.addEventListener('click', () => {
  playFilm();
}));
film.addEventListener('play', () => { cover.hidden = true; });
film.addEventListener('ended', () => { cover.hidden = false; });

const signalButtons = [...document.querySelectorAll('[data-signal]')];
signalButtons.forEach(button => button.addEventListener('click', () => {
  const deviation = button.dataset.signal === 'deviation';
  signalButtons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  document.querySelector('.demo-chart').classList.toggle('deviation', deviation);
  const points = Array.from({length:241}, (_, i) => {
    const x = i * 2;
    const shift = deviation && x > 225 ? Math.min((x - 225) / 35, 1) * 30 : 0;
    const y = 65 - shift + Math.sin(x / 8) * 13 + Math.sin(x / 3.7) * 3;
    return `${i ? 'L' : 'M'}${x},${y.toFixed(2)}`;
  }).join(' ');
  document.querySelector('#demo-path').setAttribute('d', points);
  document.querySelector('#demo-signal').setAttribute('aria-label', deviation ? 'Illustrative signal with a sustained upward shift from the baseline' : 'Illustrative normal electrical signal around a baseline');
  document.querySelector('#signal-status').textContent = deviation ? 'Sustained change → investigate' : 'Baseline behavior';
}));
signalButtons[0].click();

const protocols = {
  identity: { title:'The key stays with the node.', copy:'Each sensor holds its own Ed25519 signing key. The operator enrolls the public key on Pi3, so received alerts can be attributed to that node.', code:'Node private key → signed frame → enrolled public key' },
  encryption: { title:'A shared secret. Never transmitted.', copy:'X25519 and HKDF-SHA256 derive a per-node, per-day key. ChaCha20-Poly1305 encrypts the payload and authenticates its header before the frame crosses the radio link.', code:'X25519 → HKDF-SHA256 → ChaCha20-Poly1305' },
  verification: { title:'Trust is earned at the receiver.', copy:'Pi3 checks enrollment and the Ed25519 signature, authenticates and decrypts the payload, then rejects previously seen epoch, boot, and counter tuples.', code:'Enrolled identity → signature → decryption → replay guard' }
};
const tabs = [...document.querySelectorAll('[data-protocol]')];
function selectTab(tab) {
  tabs.forEach(item => { item.setAttribute('aria-selected',String(item === tab)); item.tabIndex = item === tab ? 0 : -1; });
  const content = protocols[tab.dataset.protocol];
  document.querySelector('#protocol-title').textContent = content.title;
  document.querySelector('#protocol-copy').textContent = content.copy;
  document.querySelector('#protocol-code').textContent = content.code;
  document.querySelector('#protocol-panel').setAttribute('aria-labelledby', tab.id);
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
    if (event.key === 'ArrowLeft') next = tabs[(index + tabs.length - 1) % tabs.length];
    if (event.key === 'Home') next = tabs[0];
    if (event.key === 'End') next = tabs.at(-1);
    if (next) { event.preventDefault(); selectTab(next); next.focus(); }
  });
});
