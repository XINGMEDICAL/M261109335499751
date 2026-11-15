-- Solo metadatos; sin nombres de personas, documentos ni valores contables.
-- Ejecutar en SSMS. Se valida la base de datos antes de consultar.
USE [2026 Xingmedical SAS];
GO
SELECT DB_NAME() AS BaseActual, USER_NAME() AS UsuarioActual;
SELECT s.name AS Esquema, o.name AS Tabla_o_vista,
       o.type_desc AS TipoObjeto, c.name AS Columna,
       ty.name AS TipoDato
FROM sys.objects AS o
JOIN sys.schemas AS s ON s.schema_id = o.schema_id
JOIN sys.columns AS c ON c.object_id = o.object_id
JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id
WHERE o.type IN ('U', 'V')
  AND (
       o.name LIKE '%activo%'
       OR o.name LIKE '%equipo%'
       OR o.name LIKE '%depreci%'
       OR c.name LIKE '%activo_fijo%'
       OR c.name LIKE '%activofijo%'
  )
ORDER BY s.name, o.name, c.column_id;
