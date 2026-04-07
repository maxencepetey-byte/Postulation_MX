from django import forms
from .models import ProfilUtilisateur


class ProfilForm(forms.ModelForm):
    def __init__(self, *args, required_fields=None, **kwargs):
        super().__init__(*args, **kwargs)
        if required_fields:
            for f in required_fields:
                if f in self.fields:
                    self.fields[f].required = True

    class Meta:
        model = ProfilUtilisateur
        # Liste exacte des champs présents dans ton modèle
        fields = [
            'prenom_lm', 'nom_lm', 'telephone', 'rue', 'npa', 'ville','email_lm',
            # Les textes par secteur sont gérés via `LettreSecteurTemplate`
        ]

        # Configuration visuelle avec Bootstrap
        widgets = {
            'prenom_lm': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ton Prénom'}),
            'nom_lm': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ton Nom'}),
            'email_lm': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'exemple@mail.com'}),
            'telephone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '079 000 00 00'}),
            'rue': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Rue de la Prairie 25'}),
            'npa': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '1202'}),
            'ville': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Genève'}),
        }