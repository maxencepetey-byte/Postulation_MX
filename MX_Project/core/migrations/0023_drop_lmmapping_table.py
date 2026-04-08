from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_gmailoauthtoken"),
    ]

    operations = [
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS core_lmmapping;",
            reverse_sql=migrations.RunSQL.noop,
        )
    ]

