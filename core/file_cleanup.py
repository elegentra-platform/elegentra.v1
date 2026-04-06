from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver


def _delete_field_file(instance, field_name, old_name):
    if not old_name:
        return

    model = instance.__class__
    lookup = {field_name: old_name}
    qs = model._default_manager.filter(**lookup)
    if instance.pk:
        qs = qs.exclude(pk=instance.pk)
    if qs.exists():
        return

    field = model._meta.get_field(field_name)
    storage = field.storage
    if storage.exists(old_name):
        storage.delete(old_name)


def register_file_cleanup(model, field_names):
    @receiver(pre_save, sender=model, weak=False)
    def cleanup_replaced_files(sender, instance, **kwargs):
        if not instance.pk:
            return

        try:
            previous = sender._default_manager.get(pk=instance.pk)
        except sender.DoesNotExist:
            return

        for field_name in field_names:
            old_name = getattr(previous, field_name).name if getattr(previous, field_name) else ""
            new_name = getattr(instance, field_name).name if getattr(instance, field_name) else ""
            if old_name and old_name != new_name:
                _delete_field_file(instance, field_name, old_name)

    @receiver(post_delete, sender=model, weak=False)
    def cleanup_deleted_files(sender, instance, **kwargs):
        for field_name in field_names:
            file_field = getattr(instance, field_name, None)
            old_name = file_field.name if file_field else ""
            if old_name:
                _delete_field_file(instance, field_name, old_name)
