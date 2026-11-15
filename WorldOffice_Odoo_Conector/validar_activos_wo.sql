USE [2026 Xingmedical SAS];
GO
-- Diagnostico de solo lectura, sin exponer nombres, codigos ni series.
WITH Activos AS (
    SELECT IdInventario, LTRIM(RTRIM(CodigoInventario)) AS Codigo,
           Descripcion, Clasificacion
    FROM dbo.Vista_Tabla_Inventarios
    WHERE Activo = -1
      AND Descripcion_Grupo_Uno = N'ACTIVOS FIJOS'
      AND Clasificacion IN (N'Activo Fijo', N'Producto')
)
SELECT COUNT(*) AS Registros,
       COUNT(DISTINCT IdInventario) AS IDs_Distintos,
       COUNT(DISTINCT NULLIF(Codigo, N'')) AS Codigos_Distintos,
       SUM(CASE WHEN NULLIF(Codigo, N'') IS NULL THEN 1 ELSE 0 END) AS Sin_Codigo,
       SUM(CASE WHEN NULLIF(LTRIM(RTRIM(Descripcion)), N'') IS NULL THEN 1 ELSE 0 END) AS Sin_Descripcion,
       SUM(CASE WHEN Clasificacion = N'Activo Fijo' THEN 1 ELSE 0 END) AS Clasificados_Activo_Fijo,
       SUM(CASE WHEN Clasificacion = N'Producto' THEN 1 ELSE 0 END) AS Clasificados_Producto
FROM Activos;
