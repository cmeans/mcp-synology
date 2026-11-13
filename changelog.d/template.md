{#- Fragments are written verbatim, bullet included ("- **Title** (#N): ..."), so all_bullets = false
    and nothing is appended. towncrier sorts a category's entries by the issue number in each
    fragment's file name, oldest first; |reverse makes it newest first. -#}
{% for section, _ in sections.items() %}

{% for category, val in definitions.items() if category in sections[section] %}
{% if category == "release" %}
{% for text, values in sections[section][category].items() %}
{{ text }}

{% endfor %}
{% else %}
### {{ definitions[category]['name'] }}

{% for text, values in sections[section][category].items()|list|reverse %}
{{ text }}
{% endfor %}

{% endif %}
{% endfor %}
{% endfor %}
