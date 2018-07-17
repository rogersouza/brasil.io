from textwrap import dedent
from urllib.parse import urlparse

from django.contrib.postgres.fields import ArrayField, JSONField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import (SearchQuery, SearchVector,
                                            SearchVectorField)
from django.db import connection, models


DYNAMIC_MODEL_REGISTRY = {}
FIELD_TYPES = {
    'binary': models.BinaryField,
    'bool': models.BooleanField,
    'date': models.DateField,
    'datetime': models.DateTimeField,
    'decimal': models.DecimalField,
    'email': models.EmailField,
    'float': models.FloatField,
    'integer': models.IntegerField,
    'json': JSONField,
    'string': models.CharField,
    'text': models.TextField,
}


class DynamicModelMixin:

    @classmethod
    def create_table(cls, create_indexes=True):
        indexes = cls._meta.indexes
        if not create_indexes:
            cls._meta.indexes = []
        try:
            with connection.schema_editor() as schema_editor:
                schema_editor.create_model(cls)
        except:
            raise
        finally:
            # TODO: check if this approach really works - it looks like Django
            # is caching something so the model class must be recreated - see
            # Table.get_model comments.
            cls._meta.indexes = indexes

    @classmethod
    def analyse_table(cls):
        with connection.cursor() as cursor:
            cursor.execute('VACUUM ANALYSE {}'.format(cls._meta.db_table))

    @classmethod
    def create_indexes(cls):
        with connection.schema_editor() as schema_editor:
            for index in cls._meta.indexes:
                schema_editor.add_index(cls, index)

    @classmethod
    def delete_table(cls):
        with connection.schema_editor() as schema_editor:
            schema_editor.delete_model(cls)


class DynamicModelQuerySet(models.QuerySet):

    def apply_filters(self, filtering):
        qs = self
        model_filtering = self.model.extra['filtering']
        if model_filtering is not None:
            for field_name in model_filtering:
                value = filtering.get(field_name, None)
                if value is not None:
                    qs = qs.filter(**{field_name: value})

            search_query = filtering.get('search', None)
            search_fields = self.model.extra['search']
            if search_query and search_fields:
                qs = qs.filter(search_data=SearchQuery(search_query))
        return qs

    def apply_ordering(self, query):
        qs = self
        model_ordering = self.model.extra['ordering']
        model_filtering = self.model.extra['filtering']
        allowed_fields = set(model_ordering + model_filtering)
        clean_allowed = [field.replace('-', '').strip().lower()
                         for field in allowed_fields]
        ordering_query = [field for field in query
                          if field.replace('-', '') in clean_allowed]
        if ordering_query:
            qs = qs.order_by(*ordering_query)
        elif model_ordering:
            qs = qs.order_by(*model_ordering)

        return qs


    def count(self):
        if getattr(self, '_count', None) is not None:
            return self._count

        query = self.query
        if not query.where:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT reltuples FROM pg_class WHERE relname = %s",
                        [query.model._meta.db_table],
                    )
                    self._count = int(cursor.fetchone()[0])
            except:
                self._count = super().count()
        else:
            self._count = super().count()

        return self._count

    def update_search_index(self):
        self.update(search_data=SearchVector(*self.model.extra['search']))


class Dataset(models.Model):
    author_name = models.CharField(max_length=255, null=False, blank=False)
    author_url = models.URLField(max_length=2000, null=True, blank=True)
    code_url = models.URLField(max_length=2000, null=False, blank=False)
    description = models.TextField(null=False, blank=False)
    icon = models.CharField(max_length=31, null=False, blank=False)
    license_name = models.CharField(max_length=255, null=False, blank=False)
    license_url = models.URLField(max_length=2000, null=False, blank=False)
    name = models.CharField(max_length=255, null=False, blank=False)
    show = models.BooleanField(null=False, blank=False, default=False)
    slug = models.SlugField(max_length=50, null=False, blank=False)
    source_name = models.CharField(max_length=255, null=False, blank=False)
    source_url = models.URLField(max_length=2000, null=False, blank=False)

    @property
    def tables(self):
        # By now we're ignoring version - just take the last one
        version = self.get_last_version()
        return self.table_set.filter(version=version).order_by('name')

    def get_table(self, tablename):
        return Table.objects.for_dataset(self).named(tablename)

    def get_default_table(self):
        return Table.objects.for_dataset(self).default()

    def __str__(self):
        return ('{} (by {}, source: {})'
                .format(self.name, self.author_name, self.source_name))

    def get_model_declaration(self):
        version = self.version_set.order_by('order').last()
        table = self.table_set.get(version=version, default=True)
        return table.get_model_declaration()

    def get_last_version(self):
        return self.version_set.order_by('order').last()


class Link(models.Model):
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE,
                                null=False, blank=False)
    title = models.CharField(max_length=255, null=False, blank=False)
    url = models.URLField(max_length=2000, null=False, blank=False)

    def __str__(self):
        domain = urlparse(self.url).netloc
        return '{} ({})'.format(self.title, domain)


class Version(models.Model):
    collected_at = models.DateField(null=False, blank=False)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE,
                                null=False, blank=False)
    download_url = models.URLField(max_length=2000, null=False, blank=False)
    name = models.CharField(max_length=255, null=False, blank=False)
    order = models.PositiveIntegerField(null=False, blank=False)

    def __str__(self):
        return ('{}.{} (order: {})'
                .format(self.dataset.slug, self.name, self.order))


class TableQuerySet(models.QuerySet):

    def for_dataset(self, dataset):
        if isinstance(dataset, str):
            kwargs = {'dataset__slug': dataset}
        else:
            kwargs = {'dataset': dataset}
        return self.filter(**kwargs)

    def default(self):
        return self.get(default=True)

    def named(self, name):
        return self.get(name=name)


class Table(models.Model):
    objects = TableQuerySet.as_manager()

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE,
                                null=False, blank=False)
    default = models.BooleanField(null=False, blank=False)
    name = models.CharField(max_length=255, null=False, blank=False)
    options = JSONField(null=True, blank=True)
    ordering = ArrayField(models.CharField(max_length=63),
                          null=False, blank=False)
    filtering = ArrayField(models.CharField(max_length=63),
                           null=True, blank=True)
    search = ArrayField(models.CharField(max_length=63),
                        null=True, blank=True)
    version = models.ForeignKey(Version, on_delete=models.CASCADE,
                                null=False, blank=False)
    last_update = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return ('{}.{}.{}'.
                format(self.dataset.slug, self.version.name, self.name))

    @property
    def db_table(self):
        return 'data_{}_{}'.format(
            self.dataset.slug.replace('-', ''),
            self.name.replace('_', ''),
        )

    @property
    def fields(self):
        return self.field_set.all()

    def get_model(self, cache=True):
        if cache and self.id in DYNAMIC_MODEL_REGISTRY:
            return DYNAMIC_MODEL_REGISTRY[self.id]

        # TODO: unregister the model in Django if already registered (self.id
        # in DYNAMIC_MODEL_REGISTRY and not cache)
        # TODO: may use Django's internal registry instead of
        # DYNAMIC_MODEL_REGISTRY
        name = self.dataset.slug + '-' + self.name.replace('_', '-')
        model_name = ''.join([word.capitalize() for word in name.split('-')])
        fields = {field.name: field.field_class
                  for field in self.fields}
        fields['search_data'] = SearchVectorField(null=True)
        ordering = self.ordering or []
        filtering = self.filtering or []
        search = self.search or []
        indexes = []
        # TODO: add has_choices fields also
        if ordering:
            indexes.append(models.Index(fields=ordering))
        if filtering:
            for field_name in filtering:
                if ordering == [field_name]:
                    continue
                indexes.append(models.Index(fields=[field_name]))
        if search:
            indexes.append(GinIndex(fields=['search_data']))

        Options = type(
            'Meta',
            (object,),
            {
                'ordering': ordering,
                'indexes': indexes,
                'db_table': self.db_table,
            },
        )
        Model = type(
            model_name,
            (DynamicModelMixin, models.Model,),
            {
                '__module__': 'core.models',
                'Meta': Options,
                'objects': DynamicModelQuerySet.as_manager(),
                **fields,
            },
        )
        Model.extra = {
            'filtering': filtering,
            'ordering': ordering,
            'search': search,
        }
        DYNAMIC_MODEL_REGISTRY[self.id] = Model
        return Model

    def get_model_declaration(self):
        model_name = ''.join([word.capitalize()
                              for word in self.dataset.slug.split('-')])
        ordering = self.ordering or []
        filtering = self.filtering or []
        search = self.search or []
        indexes = []
        if ordering:
            indexes.append(ordering)
        if filtering:
            indexes.extend(filtering)
        # TODO: add search indexes
        fields_text = []
        for field in self.fields:
            kwargs = field.options or {}
            kwargs['null'] = field.null
            field_args = ', '.join(['{}={}'.format(key, repr(value))
                                    for key, value in kwargs.items()])
            field_class = FIELD_TYPES[field.type].__name__
            fields_text.append('{} = models.{}({})'.format(field.name, field_class, field_args))
        return dedent('''
            class {model_name}(models.Model):
            {fields}

                class Meta:
                    indexes = {indexes}
                    ordering = {ordering}
        ''').format(model_name=model_name, indexes=repr(indexes), ordering=repr(ordering),
                    fields='    ' + '\n    '.join(fields_text))
        # TODO: missing objects?


class FieldQuerySet(models.QuerySet):

    def for_table(self, table):
        return self.filter(table=table)

    def choiceables(self):
        return self.filter(has_choices=True, show_on_frontend=True)


class Field(models.Model):
    objects = FieldQuerySet.as_manager()

    TYPE_CHOICES = [(value, value) for value in FIELD_TYPES.keys()]

    choices = JSONField(null=True, blank=True)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE,
                                null=False, blank=False)
    description = models.CharField(max_length=255, null=True, blank=True)
    frontend_filter = models.BooleanField(null=False, blank=True, default=False)
    has_choices = models.BooleanField(null=False, blank=True, default=False)
    link_template = models.URLField(max_length=2000, null=True, blank=True)
    order = models.PositiveIntegerField(null=False, blank=False)
    null = models.BooleanField(null=False, blank=True, default=True)
    name = models.CharField(max_length=63)
    options = JSONField(null=True, blank=True)
    obfuscate = models.BooleanField(null=False, blank=True, default=False)
    show = models.BooleanField(null=False, blank=True, default=True)
    show_on_frontend = models.BooleanField(null=False, blank=True, default=False)
    table = models.ForeignKey(Table, on_delete=models.CASCADE,
                              null=False, blank=False)
    title = models.CharField(max_length=63)
    type = models.CharField(max_length=15, choices=TYPE_CHOICES,
                            null=False, blank=False)
    version = models.ForeignKey(Version, on_delete=models.CASCADE,
                                null=True, blank=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        options = self.options or {}
        options_str = ', '.join('{}={}'.format(key, repr(value))
                                               for key, value in options.items())
        return '{}.{}({})'.format(self.table.name, self.name, options_str)

    @property
    def field_class(self):
        kwargs = self.options or {}
        kwargs['null'] = self.null
        return FIELD_TYPES[self.type](**kwargs)

    def options_text(self):
        if not self.options:
            return ''

        return ', '.join(['{}={}'.format(key, repr(value))
                          for key, value in self.options.items()])

    def update_choices(self):
        Model = self.table.get_model()
        choices = Model.objects.order_by(self.name)\
                               .distinct(self.name)\
                               .values_list(self.name, flat=True)
        self.choices = {'data': [str(value) for value in choices]}
