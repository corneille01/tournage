SELECT
    table_name,
    column_name,
    data_type,
    udt_name
FROM information_schema.columns
WHERE table_name IN (
    'paysage_projets',
    'paysage_scenes',
    'paysages',
    'pelify_users'
)
AND column_name = 'id'
ORDER BY table_name;