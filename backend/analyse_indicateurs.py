"""Indicateurs dynamiques de l'observatoire ciné-touristique."""
from collections import defaultdict
from statistics import mean
from db import fetch_all

ISO_MINUTES=(5,10,15,30)

def _pct(a,b): return round(100*a/b,1) if b else 0.0

async def construire_indicateurs_cinetourisme(region="Occitanie"):
    lieux=await fetch_all("""
      SELECT lt.id,lt.departement,lt.commune,lt.latitude,lt.longitude,
             f.id AS film_id,f.titre,f.annee,f.media_type,
             COALESCE(f.popularity,0) AS popularite
      FROM lieux_tournage lt JOIN films f ON f.id=lt.film_id
      WHERE f.region=%s AND f.statut='publie'
        AND lt.latitude IS NOT NULL AND lt.longitude IS NOT NULL
      ORDER BY lt.id
    """,(region,))
    if not lieux:
        return {"region":region,"totaux":{"nb_films":0,"nb_lieux":0,"nb_departements":0},
          "departements":[],"concentration":{"hhi":0,"top3_pct":0},
          "accessibilite":{"pret_15_pct":0,"pret_30_pct":0,"isoles_45_pct":0},
          "isochrones":{"couverture_pct":0,"couverture_par_minutes":[]},
          "opportunites":[],"vigilances":[],"lieux_potentiel":[],"films_notables":[],
          "completude":{"coordinates_pct":0,"amenities_pct":0,"popularite_pct":0},
          "scores":{"opportunite_regionale":0,"concentration_label":"aucune donnée"},
          "methodologie":{"texte":"Aucune donnée publiée géolocalisée disponible."}}
    ids=[x["id"] for x in lieux]; access=defaultdict(dict)
    amen=await fetch_all("""SELECT lieu_tournage_id,categorie,
      MIN(duree_voiture_secondes) AS voiture_s,MIN(duree_pied_secondes) AS pied_s
      FROM amenity_cache WHERE lieu_tournage_id=ANY(%s)
      AND categorie IN ('hebergement','restaurant')
      GROUP BY lieu_tournage_id,categorie""",(ids,))
    for a in amen: access[a["lieu_tournage_id"]][a["categorie"]]=a

    deps=defaultdict(lambda:{"lieux":[],"films":set(),"pop":[]})
    for l in lieux:
        d=l["departement"] or "Non renseigné";deps[d]["lieux"].append(l);deps[d]["films"].add(l["film_id"]);deps[d]["pop"].append(float(l["popularite"] or 0))
    total=len(lieux); rows=[]
    for dep,v in deps.items():
        r15=r30=isol=0;hs=[];rs=[]
        for l in v["lieux"]:
            a=access.get(l["id"],{});h=a.get("hebergement");r=a.get("restaurant")
            hv=h.get("voiture_s") if h else None;rv=r.get("voiture_s") if r else None
            if hv is not None:hs.append(hv/60)
            if rv is not None:rs.append(rv/60)
            if hv is not None and rv is not None:
                r15+=hv<=900 and rv<=900;r30+=hv<=1800 and rv<=1800
            if hv is None or hv>2700:isol+=1
        rows.append({"departement":dep,"nb_lieux":len(v["lieux"]),"nb_films":len(v["films"]),
          "part_pourcentage":_pct(len(v["lieux"]),total),"part_brute":len(v["lieux"])/total,
          "popularite_moyenne":round(mean(v["pop"]),1) if v["pop"] else 0,
          "moy_hebergement":round(mean(hs),1) if hs else None,"moy_restaurant":round(mean(rs),1) if rs else None,
          "pret_15_pct":_pct(r15,len(v["lieux"])),"pret_30_pct":_pct(r30,len(v["lieux"])),"isoles_45_pct":_pct(isol,len(v["lieux"]))})
    rows.sort(key=lambda x:x["nb_lieux"],reverse=True);cum=0
    for d in rows:cum+=d["part_brute"];d["part_cumulee_pct"]=round(cum*100,1)
    hhi=10000*sum(d["part_brute"]**2 for d in rows);top3=100*sum(d["part_brute"] for d in rows[:3])
    maxpop=max((d["popularite_moyenne"] for d in rows),default=1)
    for d in rows:
        concentration=min(100,d["part_pourcentage"]*5);readiness=.65*d["pret_15_pct"]+.35*d["pret_30_pct"];pop=min(100,100*d["popularite_moyenne"]/maxpop if maxpop else 0)
        d["score_opportunite"]=round(.4*concentration+.35*readiness+.25*pop,1)
        d["score_vigilance"]=round(.45*concentration+.55*d["isoles_45_pct"],1)
        d["recommandation"]="Priorité de valorisation : forte présence et environnement relativement prêt." if d["score_opportunite"]>=60 else ("Territoire à renforcer : présence observée mais accessibilité touristique à améliorer." if d["score_vigilance"]>=55 else "Potentiel à structurer : présence intéressante, préparation intermédiaire.")
    ready15=sum(1 for l in lieux if access.get(l["id"],{}).get("hebergement",{}).get("voiture_s") is not None and access.get(l["id"],{}).get("restaurant",{}).get("voiture_s") is not None and access[l["id"]]["hebergement"]["voiture_s"]<=900 and access[l["id"]]["restaurant"]["voiture_s"]<=900)
    ready30=sum(1 for l in lieux if access.get(l["id"],{}).get("hebergement",{}).get("voiture_s") is not None and access.get(l["id"],{}).get("restaurant",{}).get("voiture_s") is not None and access[l["id"]]["hebergement"]["voiture_s"]<=1800 and access[l["id"]]["restaurant"]["voiture_s"]<=1800)
    isolated=sum(1 for l in lieux if access.get(l["id"],{}).get("hebergement",{}).get("voiture_s") is None or access[l["id"]]["hebergement"]["voiture_s"]>2700)

    iso=await fetch_all("""SELECT lieu_tournage_id,mode,minutes,geometry_geojson,calculated_at
      FROM isochrones WHERE lieu_tournage_id=ANY(%s) AND minutes=ANY(%s)""",(ids,list(ISO_MINUTES)))
    im={(x["lieu_tournage_id"],x["mode"],x["minutes"]):x for x in iso};cov=[]
    for m in ISO_MINUTES:
        car=sum((lid,"driving-car",m) in im for lid in ids);foot=sum((lid,"foot-walking",m) in im for lid in ids)
        cov.append({"minutes":m,"voiture_pct":_pct(car,total),"pied_pct":_pct(foot,total)})
    ratio=await fetch_all("""SELECT AVG(
      ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(c.geometry_geojson::text),4326)::geography) /
      NULLIF(ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(p.geometry_geojson::text),4326)::geography),0))
      AS ratio FROM isochrones c JOIN isochrones p
      ON p.lieu_tournage_id=c.lieu_tournage_id AND p.minutes=c.minutes
      WHERE c.lieu_tournage_id=ANY(%s) AND c.mode='driving-car'
      AND p.mode='foot-walking' AND c.minutes=15""",(ids,))
    ratio=ratio[0]["ratio"] if ratio else None
    points=[]
    for l in sorted(lieux,key=lambda x:float(x["popularite"] or 0),reverse=True)[:100]:
        a=access.get(l["id"],{});h=a.get("hebergement",{});r=a.get("restaurant",{})
        t=max(h.get("voiture_s") or 99999,r.get("voiture_s") or 99999);prep=max(0,100-min(100,t/1800*100))
        points.append({"titre":l["titre"],"departement":l["departement"],"popularite":round(float(l["popularite"] or 0),1),"preparation":round(prep,1)})
    popularite_pct=_pct(sum(float(l["popularite"] or 0)>0 for l in lieux),total)
    return {"region":region,"totaux":{"nb_films":len({l["film_id"] for l in lieux}),"nb_lieux":total,"nb_departements":len(rows)},
      "departements":rows,"concentration":{"hhi":round(hhi,1),"top3_pct":round(top3,1)},
      "accessibilite":{"pret_15_pct":_pct(ready15,total),"pret_30_pct":_pct(ready30,total),"isoles_45_pct":_pct(isolated,total)},
      "isochrones":{"couverture_pct":round(mean([max(x["voiture_pct"],x["pied_pct"]) for x in cov]),1) if cov else 0,"couverture_par_minutes":cov,
        "ratio_surface_voiture_marche_15":round(float(ratio),2) if ratio is not None else None,"derniere_date":max((x["calculated_at"] for x in iso),default=None)},
      "opportunites":[{"departement":x["departement"],"score":x["score_opportunite"],"interpretation":x["recommandation"]} for x in sorted(rows,key=lambda x:x["score_opportunite"],reverse=True)],
      "vigilances":[{"departement":x["departement"],"score":x["score_vigilance"],"interpretation":x["recommandation"]} for x in sorted(rows,key=lambda x:x["score_vigilance"],reverse=True)],
      "lieux_potentiel":points,"films_notables":[],
      "completude":{"coordinates_pct":100.0,"amenities_pct":round(_pct(len({a["lieu_tournage_id"] for a in amen}),total),1),"popularite_pct":round(popularite_pct,1)},
      "scores":{"opportunite_regionale":round(mean(x["score_opportunite"] for x in rows),1),"concentration_label":"répartition dispersée" if hhi<1500 else ("concentration intermédiaire" if hhi<2500 else "forte concentration")},
      "methodologie":{"texte":"Indicateurs calculés dynamiquement depuis les lieux publiés géolocalisés, amenity_cache et les isochrones IGN pré-calculés. Les scores servent à prioriser l’analyse et ne constituent pas une mesure officielle du potentiel touristique."}}
