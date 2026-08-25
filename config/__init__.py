import pymysql

# Django 6.x requires mysqlclient >= 2.2.1; PyMySQL emulates MySQLdb for local/Windows installs.
pymysql.version_info = (2, 2, 1, "final", 0)
pymysql.install_as_MySQLdb()
