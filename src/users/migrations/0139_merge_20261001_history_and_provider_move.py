from django.db import migrations


class Migration(migrations.Migration):
    """Join the saved-history-views and TV-provider-move migration branches."""

    dependencies = [
        ("users", "0138_saved_view_history"),
        ("users", "0138_user_tv_auto_move_to_default_provider"),
    ]

    operations = []
