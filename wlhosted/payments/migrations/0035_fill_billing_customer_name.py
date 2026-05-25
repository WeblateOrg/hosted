from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        (
            "payments",
            "0034_customer_vat_validated_customer_vat_validation_error_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop),
    ]
