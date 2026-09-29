/* correctifs_personnalisation_frontend.js — Étape 1
 *
 * CAUSE DU « HTML QUI S'AFFICHE » :
 * construireScenarioCinetouristique() fabriquait des chaînes contenant du HTML
 * (<i class="fa-solid …">), puis afficherResultatMonParcours() les passait dans
 * escapeHtml() → les balises <i> étaient échappées et apparaissaient en clair
 * dans « Votre scénario conseillé ».
 *
 * Correction propre : la fonction renvoie {icone, texte} ; le texte est échappé,
 * l'icône est construite par le rendu. Aucun HTML n'est plus stocké dans les
 * données, donc on n'a pas besoin de désactiver l'échappement (ce qui aurait
 * ouvert une faille XSS : g.nom, etape.nom, r.nom viennent de la base).
 *
 * 1) REMPLACER la fonction construireScenarioCinetouristique (app.js ~l.348) :
 */
function construireScenarioCinetouristique(data){
  const guidesDisponibles=data.visites_guidees_disponibles||[];
  const guidesPlanifies=data.visites_guidees_planifiees||[];
  const tempsBase=Number(data.temps_disponible_minutes||0);
  const tempsUtilise=Number(data.duree_totale_estimee_secondes||0)/60;
  const scenario=[];
  const ajouter=(icone,texte)=>{ if(texte) scenario.push({icone,texte:String(texte)}); };

  if(data.date_sortie){
    ajouter('fa-calendar-days',`Sortie prévue le ${new Date(data.date_sortie+'T12:00:00').toLocaleDateString('fr-FR')}, à partir de ${data.heure_depart||'09:00'}.`);
  }
  if(data.scenario_recommande?.message) ajouter('fa-bullseye',data.scenario_recommande.message);

  if(guidesPlanifies.length){
    const g=guidesPlanifies[0];
    ajouter('fa-ticket',`${g.nom} est le rendez-vous guidé le plus directement intégrable à vos critères pour cette date.`);
    if(g.heure_debut) ajouter('fa-clock',`Pelify vous conseille de viser le créneau ${g.heure_debut}–${g.heure_fin||''}. L'étape concernée est placée en priorité dans le scénario.`);
    if(g.lien) ajouter('fa-circle-info',`Réservation obligatoire ou recommandée : vérifiez le créneau sur la page officielle avant de partir.`);
  }else if(guidesDisponibles.length){
    ajouter('fa-ticket',`${guidesDisponibles.length} créneau(x) de visite guidée sont référencés pour cette date. Pelify peut les afficher, mais ne confirme jamais une disponibilité de réservation.`);
  }else if(data.inclure_visites_guidees!==false){
    ajouter('fa-ticket',`Aucune disponibilité datée n'est connue dans le référentiel Pelify pour cette date ; les visites éventuellement proposées restent consultables via leurs pages officielles.`);
  }

  if(tempsBase){
    const marge=Math.round(tempsBase-tempsUtilise);
    if(marge>=0) ajouter('fa-circle-check',`Avec vos critères, il resterait environ ${marge} min de marge estimée.`);
    else ajouter('fa-triangle-exclamation',`Votre sélection dépasse votre créneau d’environ ${Math.abs(marge)} min.`);
  }

  // Jusqu'à 3 suggestions (une par étape, dans l'ordre du parcours) au lieu d'une seule.
  const reco=data.recommandations_par_etape||{};
  let nbSuggestions=0;
  for(const etape of (data.etapes||[])){
    if(nbSuggestions>=3) break;
    const r=(reco[String(etape.id)]||[])[0];
    if(!r) continue;
    ajouter('fa-lightbulb',`Pour « ${etape.nom||'votre étape'} », Pelify vous suggère ${r.nom||'une offre à proximité'} : ${r.raison||'elle correspond à vos critères'}`);
    nbSuggestions++;
  }
  return {guides:guidesDisponibles,texte:scenario,tempsUtilise};
}

/*
 * 2) Dans afficherResultatMonParcours (app.js ~l.443), REMPLACER la ligne
 *    « const conseil=… » par :
 */
const conseil=scenario.texte.length
  ? `<section class="mp-scenario"><h3><i class="fa-solid fa-film" aria-hidden="true"></i> Votre scénario conseillé</h3>${
      scenario.texte.map(x=>`<p><i class="fa-solid ${escapeAttr(x.icone)}" aria-hidden="true"></i> ${escapeHtml(x.texte)}</p>`).join('')
    }</section>`
  : '';

/*
 * 3) Dans calculerMonParcoursGlobal (~l.335), la clé budget_max_euros est écrite
 *    deux fois dans `body` : supprimer la seconde occurrence (sans effet, mais trompeur).
 */