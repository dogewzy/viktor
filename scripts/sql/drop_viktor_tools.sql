-- 废弃能力清理：删除已不再使用的 SQL 模板工具表。
-- 升级已有 Viktor 元数据库时执行一次即可。
DROP TABLE IF EXISTS `viktor_tools`;
