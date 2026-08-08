from django.db import models

class Company(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=11)                        # 'ECHO-4821'
    code_normalized = models.CharField(max_length=10, unique=True, db_index=True)  # 'ECHO4821'
    created_at = models.DateTimeField(auto_now_add=True)