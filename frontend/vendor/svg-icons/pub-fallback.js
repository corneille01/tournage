// Icônes SVG de repli (si l'image Awin ne charge pas) — inline, pas de fichier externe à charger
const PUB_SVG_FALLBACK = {
  voiture: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M5 11l1.5-4.5A2 2 0 0 1 8.4 5h7.2a2 2 0 0 1 1.9 1.5L19 11h1a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-1v1a1 1 0 0 1-1 1h-1a1 1 0 0 1-1-1v-1H7v1a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-1H3a1 1 0 0 1-1-1v-5a1 1 0 0 1 1-1h1zm2 0h10l-1-3H8l-1 3zM6 15a1 1 0 1 0 0-2 1 1 0 0 0 0 2zm12 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2z"/></svg>',
  parking: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1.5 5h2.8a3.2 3.2 0 0 1 0 6.4H11v3.6H9.5V7zM11 8.5v3.4h2.3a1.7 1.7 0 0 0 0-3.4H11z"/></svg>',
  valise: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M9 4a2 2 0 0 0-2 2v1H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-2V6a2 2 0 0 0-2-2H9zm0 3V6h6v1H9zM5 9h14v9H5V9z"/></svg>',
  avion: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-3 2v1.5l4.5-1.5 4.5 1.5V21l-3-2v-5.5z"/></svg>',
  casque: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3a8 8 0 0 0-8 8v6a3 3 0 0 0 3 3h1v-8H6v-1a6 6 0 0 1 12 0v1h-2v8h1a3 3 0 0 0 3-3v-6a8 8 0 0 0-8-8z"/></svg>',
  bouclier: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l8 3v6c0 5-3.4 9-8 11-4.6-2-8-6-8-11V5l8-3z"/></svg>',
};
