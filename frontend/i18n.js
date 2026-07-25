// ═══════════════════════════════════════════════════════════════
// Pelify Lieux de tournage — i18n.js
// Traduction de l'INTERFACE (boutons, libellés) selon la langue du
// navigateur, comme sur Pelify. Le contenu éditorial (synopsis,
// anecdotes) reste en français pour l'instant — chantier séparé.
// ═══════════════════════════════════════════════════════════════

const TRADUCTIONS = {
  fr: {
    sousTitre: "Lieux de tournage en Occitanie",
    recherchePlaceholder: "Chercher un film, une série…",
    tout: "Tout", films: "Films", series: "Séries",
    annee: "Année", departement: "Département", nationalite: "Nationalité",
    plusConnus: "Les plus connus",
    analyseComplete: "Analyse territoriale complète",
    ouDormir: "Où dormir", ouManger: "Où manger",
    officeTourisme: "Office de tourisme", seGarer: "Se garer",
    disponibleSur: "Disponible sur :",
    voulezVousVisiter: "Vous voulez visiter ce lieu ? Voici ce qu'il vous faut savoir :",
    surLesTraces: "Sur les traces de ce film…",
    recentrer: "Recentrer",
    departements: "Départements", carteChaleur: "Carte de chaleur",
    aPied: "À pied", enVoiture: "En voiture",
    chargement: "Chargement…",
  },
  en: {
    sousTitre: "Filming locations in Occitanie",
    recherchePlaceholder: "Search a movie, a series…",
    tout: "All", films: "Movies", series: "TV Series",
    annee: "Year", departement: "Department", nationalite: "Nationality",
    plusConnus: "Most popular",
    analyseComplete: "Full territorial analysis",
    ouDormir: "Where to sleep", ouManger: "Where to eat",
    officeTourisme: "Tourist office", seGarer: "Parking",
    disponibleSur: "Available on:",
    voulezVousVisiter: "Want to visit this location? Here's what you need to know:",
    surLesTraces: "Follow the trail of this movie…",
    recentrer: "Recenter",
    departements: "Departments", carteChaleur: "Heat map",
    aPied: "Walking", enVoiture: "Driving",
    chargement: "Loading…",
  },
  es: {
    sousTitre: "Lugares de rodaje en Occitania",
    recherchePlaceholder: "Buscar una película, una serie…",
    tout: "Todo", films: "Películas", series: "Series",
    annee: "Año", departement: "Departamento", nationalite: "Nacionalidad",
    plusConnus: "Más populares",
    analyseComplete: "Análisis territorial completo",
    ouDormir: "Dónde dormir", ouManger: "Dónde comer",
    officeTourisme: "Oficina de turismo", seGarer: "Aparcamiento",
    disponibleSur: "Disponible en:",
    voulezVousVisiter: "¿Quieres visitar este lugar? Esto es lo que necesitas saber:",
    surLesTraces: "Sigue el rastro de esta película…",
    recentrer: "Recentrar",
    departements: "Departamentos", carteChaleur: "Mapa de calor",
    aPied: "A pie", enVoiture: "En coche",
    chargement: "Cargando…",
  },
  de: {
    sousTitre: "Drehorte in Okzitanien",
    recherchePlaceholder: "Film oder Serie suchen…",
    tout: "Alle", films: "Filme", series: "Serien",
    annee: "Jahr", departement: "Département", nationalite: "Nationalität",
    plusConnus: "Am bekanntesten",
    analyseComplete: "Vollständige Gebietsanalyse",
    ouDormir: "Übernachten", ouManger: "Essen",
    officeTourisme: "Touristeninformation", seGarer: "Parken",
    disponibleSur: "Verfügbar auf:",
    voulezVousVisiter: "Diesen Ort besuchen? Das musst du wissen:",
    surLesTraces: "Auf den Spuren dieses Films…",
    recentrer: "Zentrieren",
    departements: "Départements", carteChaleur: "Heatmap",
    aPied: "Zu Fuß", enVoiture: "Mit dem Auto",
    chargement: "Lädt…",
  },
  zh: {
    sousTitre: "奥克西塔尼大区拍摄地",
    recherchePlaceholder: "搜索电影或剧集…",
    tout: "全部", films: "电影", series: "剧集",
    annee: "年份", departement: "省份", nationalite: "国籍",
    plusConnus: "最受欢迎",
    analyseComplete: "完整区域分析",
    ouDormir: "住宿", ouManger: "餐饮",
    officeTourisme: "旅游咨询处", seGarer: "停车",
    disponibleSur: "观看平台：",
    voulezVousVisiter: "想去这个地方吗？以下是你需要知道的：",
    surLesTraces: "追随这部电影的足迹…",
    recentrer: "重新居中",
    departements: "省份", carteChaleur: "热力图",
    aPied: "步行", enVoiture: "驾车",
    chargement: "加载中…",
  },
};

// Langue détectée depuis le navigateur — repli sur le français si la
// langue n'est pas (encore) couverte par nos traductions.
function _detecterLangue() {
  const codes = navigator.languages || [navigator.language || "fr"];
  for (const code of codes) {
    const base = code.toLowerCase().split("-")[0];
    if (TRADUCTIONS[base]) return base;
  }
  return "fr";
}

const langueCourante = _detecterLangue();

function t(cle) {
  return (TRADUCTIONS[langueCourante] && TRADUCTIONS[langueCourante][cle])
    || TRADUCTIONS.fr[cle]
    || cle;
}

// Applique les traductions à tous les éléments marqués data-i18n dans le HTML
function appliquerTraductions() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const cle = el.dataset.i18n;
    if (el.hasAttribute("placeholder")) {
      el.setAttribute("placeholder", t(cle));
    } else {
      el.textContent = t(cle);
    }
  });
  document.documentElement.lang = langueCourante;
}

document.addEventListener("DOMContentLoaded", appliquerTraductions);
