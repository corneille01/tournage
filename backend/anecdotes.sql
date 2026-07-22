-- Château de Ferrières (issu du .sql généré, propre tel quel)
UPDATE lieux_tournage SET anecdote = 'France :
Seine-et Marne
Château de Ferrières, Ferrières-en-Brie

(Source : Wikipédia, CC BY-SA)' WHERE id = 237;

-- Tout nous sépare (Sète + Perpignan, même texte pour les deux lieux)
UPDATE lieux_tournage 
SET anecdote = 'Le film a été tourné entre juin et juillet 2016. Les scènes ont été essentiellement tournées à Sète et à Perpignan où Thierry Kilfa et l''équipe du tournage se sont rendus physiquement. (Source : Wikipédia, CC BY-SA)' 
WHERE id IN (80, 138);

-- Le Pacte des loups — cathédrale de Lectoure (reformulé à la main, pas la liste brute)
UPDATE lieux_tournage 
SET anecdote = 'La cathédrale Saint-Gervais-Saint-Protais de Lectoure, dans le Gers, et ses remparts ont servi de décor lors du tournage du Pacte des loups. (Source : Wikipédia, CC BY-SA)' 
WHERE id = 188;