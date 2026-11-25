DELETE FROM processing_jobs WHERE kind='weather';
DROP TABLE IF EXISTS weather_slices;
DROP TABLE IF EXISTS weather_cache;
