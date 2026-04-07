from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0019_lettresecteurtemplate_email_fields"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="lettresecteurtemplate",
            name="email_subject",
        ),
        migrations.RemoveField(
            model_name="lettresecteurtemplate",
            name="email_body",
        ),
    ]

