# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.

# Python
import codecs
import datetime
import logging
import os
import time
import json
from urllib.parse import urljoin


# Django
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.query import QuerySet

# from django.core.cache import cache
from django.utils.encoding import smart_str
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import FieldDoesNotExist

# REST Framework
from rest_framework.exceptions import ParseError

# AWX
from awx.api.versioning import reverse
from awx.main.models.base import (
    BaseModel,
    CreatedModifiedModel,
    prevent_search,
    accepts_json,
    JOB_TYPE_CHOICES,
    NEW_JOB_TYPE_CHOICES,
    VERBOSITY_CHOICES,
    VarsDictProperty,
)
from awx.main.models.events import JobEvent, UnpartitionedJobEvent, UnpartitionedSystemJobEvent, SystemJobEvent
from awx.main.models.unified_jobs import UnifiedJobTemplate, UnifiedJob
from awx.main.models.notifications import (
    NotificationTemplate,
    JobNotificationMixin,
)
from awx.main.utils import parse_yaml_or_json, getattr_dne, NullablePromptPseudoField, polymorphic, log_excess_runtime
from awx.main.fields import ImplicitRoleField, AskForField, JSONBlob, OrderedManyToManyField
from awx.main.models.mixins import (
    ResourceMixin,
    SurveyJobTemplateMixin,
    SurveyJobMixin,
    TaskManagerJobMixin,
    CustomVirtualEnvMixin,
    RelatedJobsMixin,
    WebhookMixin,
    WebhookTemplateMixin,
)
from awx.main.constants import JOB_VARIABLE_PREFIXES


logger = logging.getLogger('awx.main.models.jobs')
analytics_logger = logging.getLogger('awx.analytics.job_events')
system_tracking_logger = logging.getLogger('awx.analytics.system_tracking')

__all__ = ['JobTemplate', 'JobLaunchConfig', 'Job', 'JobHostSummary', 'SystemJobTemplate', 'SystemJob']


class JobOptions(BaseModel):
    """
    Common options for job templates and jobs.
    """

    class Meta:
        abstract = True

    diff_mode = models.BooleanField(
        default=False,
        help_text=_("If enabled, textual changes made to any templated files on the host are shown in the standard output"),
    )
    job_type = models.CharField(
        max_length=64,
        choices=JOB_TYPE_CHOICES,
        default='run',
    )
    inventory = models.ForeignKey(
        'Inventory',
        related_name='%(class)ss',
        blank=True,
        null=True,
        default=None,
        on_delete=models.SET_NULL,
    )
    project = models.ForeignKey(
        'Project',
        related_name='%(class)ss',
        null=True,
        default=None,
        blank=True,
        on_delete=models.SET_NULL,
    )
    playbook = models.CharField(
        max_length=1024,
        default='',
        blank=True,
    )
    scm_branch = models.CharField(
        max_length=1024,
        default='',
        blank=True,
        help_text=_('Branch to use in job run. Project default used if blank. ' 'Only allowed if project allow_override field is set to true.'),
    )
    forks = models.PositiveIntegerField(
        blank=True,
        default=0,
    )
    limit = models.TextField(
        blank=True,
        default='',
    )
    verbosity = models.PositiveIntegerField(
        choices=VERBOSITY_CHOICES,
        blank=True,
        default=0,
    )
    extra_vars = prevent_search(
        accepts_json(
            models.TextField(
                blank=True,
                default='',
            )
        )
    )
    job_tags = models.TextField(
        blank=True,
        default='',
    )
    force_handlers = models.BooleanField(
        blank=True,
        default=False,
    )
    skip_tags = models.CharField(
        max_length=1024,
        blank=True,
        default='',
    )
    start_at_task = models.CharField(
        max_length=1024,
        blank=True,
        default='',
    )
    become_enabled = models.BooleanField(
        default=False,
    )
    allow_simultaneous = models.BooleanField(
        default=False,
    )
    timeout = models.IntegerField(
        blank=True,
        default=0,
        help_text=_("The amount of time (in seconds) to run before the task is canceled."),
    )
    use_fact_cache = models.BooleanField(
        default=False,
        help_text=_(
            "If enabled, the service will act as an Ansible Fact Cache Plugin; persisting "
            "facts at the end of a playbook run to the database and caching facts for use by Ansible."
        ),
    )

    extra_vars_dict = VarsDictProperty('extra_vars', True)

    @property
    def machine_credential(self):
        return self.credentials.filter(credential_type__kind='ssh').first()

    @property
    def network_credentials(self):
        return list(self.credentials.filter(credential_type__kind='net'))

    @property
    def cloud_credentials(self):
        return list(self.credentials.filter(credential_type__kind='cloud'))

    @property
    def vault_credentials(self):
        return list(self.credentials.filter(credential_type__kind='vault'))

    @property
    def passwords_needed_to_start(self):
        '''Return list of password field names needed to start the job.'''
        needed = []
        # Unsaved credential objects can not require passwords
        if not self.pk:
            return needed
        for cred in self.credentials.all():
            needed.extend(cred.passwords_needed)
        return needed


class JobTemplate(UnifiedJobTemplate, JobOptions, SurveyJobTemplateMixin, ResourceMixin, CustomVirtualEnvMixin, RelatedJobsMixin, WebhookTemplateMixin):
    """
    A job template is a reusable job definition for applying a project (with
    playbook) to an inventory source with a given credential.
    """

    FIELDS_TO_PRESERVE_AT_COPY = ['labels', 'instance_groups', 'credentials', 'survey_spec', 'prevent_instance_group_fallback']
    FIELDS_TO_DISCARD_AT_COPY = ['vault_credential', 'credential']
    SOFT_UNIQUE_TOGETHER = [('polymorphic_ctype', 'name', 'organization')]

    class Meta:
        app_label = 'main'
        ordering = ('name',)

    job_type = models.CharField(
        max_length=64,
        choices=NEW_JOB_TYPE_CHOICES,
        default='run',
    )
    host_config_key = prevent_search(
        models.CharField(
            max_length=1024,
            blank=True,
            default='',
        )
    )
    ask_diff_mode_on_launch = AskForField(
        blank=True,
        default=False,
    )
    ask_job_type_on_launch = AskForField(
        blank=True,
        default=False,
    )
    ask_verbosity_on_launch = AskForField(
        blank=True,
        default=False,
    )
    ask_credential_on_launch = AskForField(blank=True, default=False, allows_field='credentials')
    ask_execution_environment_on_launch = AskForField(
        blank=True,
        default=False,
    )
    ask_forks_on_launch = AskForField(
        blank=True,
        default=False,
    )
    ask_job_slice_count_on_launch = AskForField(
        blank=True,
        default=False,
    )
    ask_timeout_on_launch = AskForField(
        blank=True,
        default=False,
    )
    ask_instance_groups_on_launch = AskForField(
        blank=True,
        default=False,
    )
    job_slice_count = models.PositiveIntegerField(
        blank=True,
        default=1,
        help_text=_("The number of jobs to slice into at runtime. " "Will cause the Job Template to launch a workflow if value is greater than 1."),
    )

    admin_role = ImplicitRoleField(parent_role=['organization.job_template_admin_role'])
    execute_role = ImplicitRoleField(
        parent_role=['admin_role', 'organization.execute_role'],
    )
    read_role = ImplicitRoleField(
        parent_role=[
            'organization.auditor_role',
            'inventory.organization.auditor_role',  # partial support for old inheritance via inventory
            'execute_role',
            'admin_role',
        ],
    )
    prevent_instance_group_fallback = models.BooleanField(
        default=False,
        help_text=(
            "If enabled, the job template will prevent adding any inventory or organization "
            "instance groups to the list of preferred instances groups to run on."
            "If this setting is enabled and you provided an empty list, the global instance "
            "groups will be applied."
        ),
    )

    @classmethod
    def _get_unified_job_class(cls):
        return Job

    @classmethod
    def _get_unified_job_field_names(cls):
        return set(f.name for f in JobOptions._meta.fields) | set(
            [
                'name',
                'description',
                'organization',
                'survey_passwords',
                'labels',
                'credentials',
                'job_slice_number',
                'job_slice_count',
                'execution_environment',
            ]
        )

    @property
    def validation_errors(self):
        """
        Fields needed to start, which cannot be given on launch, invalid state.
        """
        validation_errors = {}
        if self.inventory is None and not self.ask_inventory_on_launch:
            validation_errors['inventory'] = [
                _("Job Template must provide 'inventory' or allow prompting for it."),
            ]
        if self.project is None:
            validation_errors['project'] = [
                _("Job Templates must have a project assigned."),
            ]
        return validation_errors

    @property
    def resources_needed_to_start(self):
        return [fd for fd in ['project', 'inventory'] if not getattr(self, '{}_id'.format(fd))]

    def clean_forks(self):
        if settings.MAX_FORKS > 0 and self.forks > settings.MAX_FORKS:
            raise ValidationError(_(f'Maximum number of forks ({settings.MAX_FORKS}) exceeded.'))
        return self.forks

    def create_job(self, **kwargs):
        """
        Create a new job based on this template.
        """
        return self.create_unified_job(**kwargs)

    def get_effective_slice_ct(self, kwargs):
        actual_inventory = self.inventory
        if self.ask_inventory_on_launch and 'inventory' in kwargs:
            actual_inventory = kwargs['inventory']
        actual_slice_count = self.job_slice_count
        if self.ask_job_slice_count_on_launch and 'job_slice_count' in kwargs:
            actual_slice_count = kwargs['job_slice_count']
        if actual_inventory:
            return min(actual_slice_count, actual_inventory.hosts.count())
        else:
            return actual_slice_count

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields', [])
        # if project is deleted for some reason, then keep the old organization
        # to retain ownership for organization admins
        if self.project and self.project.organization_id != self.organization_id:
            self.organization_id = self.project.organization_id
            if 'organization' not in update_fields and 'organization_id' not in update_fields:
                update_fields.append('organization_id')
        return super(JobTemplate, self).save(*args, **kwargs)

    def validate_unique(self, exclude=None):
        """Custom over-ride for JT specifically
        because organization is inferred from project after full_clean is finished
        thus the organization field is not yet set when validation happens
        """
        errors = []
        for ut in JobTemplate.SOFT_UNIQUE_TOGETHER:
            kwargs = {'name': self.name}
            if self.project:
                kwargs['organization'] = self.project.organization_id
            else:
                kwargs['organization'] = None
            qs = JobTemplate.objects.filter(**kwargs)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                errors.append('%s with this (%s) combination already exists.' % (JobTemplate.__name__, ', '.join(set(ut) - {'polymorphic_ctype'})))
        if errors:
            raise ValidationError(errors)

    def create_unified_job(self, **kwargs):
        prevent_slicing = kwargs.pop('_prevent_slicing', False)
        slice_ct = self.get_effective_slice_ct(kwargs)
        slice_event = bool(slice_ct > 1 and (not prevent_slicing))
        if slice_event:
            # A Slice Job Template will generate a WorkflowJob rather than a Job
            from awx.main.models.workflow import WorkflowJobTemplate, WorkflowJobNode

            kwargs['_unified_job_class'] = WorkflowJobTemplate._get_unified_job_class()
            kwargs['_parent_field_name'] = "job_template"
            kwargs.setdefault('_eager_fields', {})
            kwargs['_eager_fields']['is_sliced_job'] = True
        elif self.job_slice_count > 1 and (not prevent_slicing):
            # Unique case where JT was set to slice but hosts not available
            kwargs.setdefault('_eager_fields', {})
            kwargs['_eager_fields']['job_slice_count'] = 1
        elif prevent_slicing:
            kwargs.setdefault('_eager_fields', {})
            kwargs['_eager_fields'].setdefault('job_slice_count', 1)
        job = super(JobTemplate, self).create_unified_job(**kwargs)
        if slice_event:
            for idx in range(slice_ct):
                create_kwargs = dict(workflow_job=job, unified_job_template=self, ancestor_artifacts=dict(job_slice=idx + 1))
                WorkflowJobNode.objects.create(**create_kwargs)
        return job

    def get_absolute_url(self, request=None):
        return reverse('api:job_template_detail', kwargs={'pk': self.pk}, request=request)

    def can_start_without_user_input(self, callback_extra_vars=None):
        """
        Return whether job template can be used to start a new job without
        requiring any user input.
        """
        variables_needed = False
        if callback_extra_vars:
            extra_vars_dict = parse_yaml_or_json(callback_extra_vars)
            for var in self.variables_needed_to_start:
                if var not in extra_vars_dict:
                    variables_needed = True
                    break
        elif self.variables_needed_to_start:
            variables_needed = True
        prompting_needed = False
        # The behavior of provisioning callback should mimic
        # that of job template launch, so prompting_needed should
        # not block a provisioning callback from creating/launching jobs.
        if callback_extra_vars is None:
            for ask_field_name in set(self.get_ask_mapping().values()):
                if getattr(self, ask_field_name):
                    prompting_needed = True
                    break
        return not prompting_needed and not self.passwords_needed_to_start and not variables_needed

    def _accept_or_ignore_job_kwargs(self, **kwargs):
        exclude_errors = kwargs.pop('_exclude_errors', [])
        prompted_data = {}
        rejected_data = {}
        accepted_vars, rejected_vars, errors_dict = self.accept_or_ignore_variables(
            kwargs.get('extra_vars', {}), _exclude_errors=exclude_errors, extra_passwords=kwargs.get('survey_passwords', {})
        )
        if accepted_vars:
            prompted_data['extra_vars'] = accepted_vars
        if rejected_vars:
            rejected_data['extra_vars'] = rejected_vars

        # Handle all the other fields that follow the simple prompting rule
        for field_name, ask_field_name in self.get_ask_mapping().items():
            if field_name not in kwargs or field_name == 'extra_vars' or kwargs[field_name] is None:
                continue

            new_value = kwargs[field_name]
            old_value = getattr(self, field_name)

            field = self._meta.get_field(field_name)
            if isinstance(field, models.ManyToManyField):
                if field_name == 'instance_groups':
                    # Instance groups are ordered so we can't make a set out of them
                    old_value = old_value.all()
                elif field_name == 'credentials':
                    # Credentials have a weird pattern because of how they are layered
                    old_value = set(old_value.all())
                    new_value = set(kwargs[field_name]) - old_value
                    if not new_value:
                        continue

            if new_value == old_value:
                # no-op case: Fields the same as template's value
                # counted as neither accepted or ignored
                continue
            elif field_name == 'scm_branch' and old_value == '' and self.project and new_value == self.project.scm_branch:
                # special case of "not provided" for branches
                # job template does not provide branch, runs with default branch
                continue
            elif getattr(self, ask_field_name):
                # Special case where prompts can be rejected based on project setting
                if field_name == 'scm_branch':
                    if not self.project:
                        rejected_data[field_name] = new_value
                        errors_dict[field_name] = _('Project is missing.')
                        continue
                    if kwargs['scm_branch'] != self.project.scm_branch and not self.project.allow_override:
                        rejected_data[field_name] = new_value
                        errors_dict[field_name] = _('Project does not allow override of branch.')
                        continue
                elif field_name == 'job_slice_count' and (new_value > 1) and (self.get_effective_slice_ct(kwargs) <= 1):
                    rejected_data[field_name] = new_value
                    errors_dict[field_name] = _('Job inventory does not have enough hosts for slicing')
                    continue
                # accepted prompt
                prompted_data[field_name] = new_value
            else:
                # unprompted - template is not configured to accept field on launch
                rejected_data[field_name] = new_value
                # Not considered an error for manual launch, to support old
                # behavior of putting them in ignored_fields and launching anyway
                if 'prompts' not in exclude_errors:
                    errors_dict[field_name] = _('Field is not configured to prompt on launch.')

        if 'prompts' not in exclude_errors and (not getattr(self, 'ask_credential_on_launch', False)) and self.passwords_needed_to_start:
            errors_dict['passwords_needed_to_start'] = _('Saved launch configurations cannot provide passwords needed to start.')

        needed = self.resources_needed_to_start
        if needed:
            needed_errors = []
            for resource in needed:
                if resource in prompted_data:
                    continue
                needed_errors.append(_("Job Template {} is missing or undefined.").format(resource))
            if needed_errors:
                errors_dict['resources_needed_to_start'] = needed_errors

        return prompted_data, rejected_data, errors_dict

    @property
    def cache_timeout_blocked(self):
        if Job.objects.filter(job_template=self, status__in=['pending', 'waiting', 'running']).count() >= getattr(settings, 'SCHEDULE_MAX_JOBS', 10):
            logger.error(
                "Job template %s could not be started because there are more than %s other jobs from that template waiting to run"
                % (self.name, getattr(settings, 'SCHEDULE_MAX_JOBS', 10))
            )
            return True
        return False

    def _can_update(self):
        return self.can_start_without_user_input()

    @property
    def notification_templates(self):
        # Return all notification_templates defined on the Job Template, on the Project, and on the Organization for each trigger type
        # TODO: Currently there is no org fk on project so this will need to be added once that is
        #       available after the rbac pr
        base_notification_templates = NotificationTemplate.objects
        error_notification_templates = list(base_notification_templates.filter(unifiedjobtemplate_notification_templates_for_errors__in=[self, self.project]))
        started_notification_templates = list(
            base_notification_templates.filter(unifiedjobtemplate_notification_templates_for_started__in=[self, self.project])
        )
        success_notification_templates = list(
            base_notification_templates.filter(unifiedjobtemplate_notification_templates_for_success__in=[self, self.project])
        )
        # Get Organization NotificationTemplates
        if self.organization is not None:
            error_notification_templates = set(
                error_notification_templates + list(base_notification_templates.filter(organization_notification_templates_for_errors=self.organization))
            )
            started_notification_templates = set(
                started_notification_templates + list(base_notification_templates.filter(organization_notification_templates_for_started=self.organization))
            )
            success_notification_templates = set(
                success_notification_templates + list(base_notification_templates.filter(organization_notification_templates_for_success=self.organization))
            )
        return dict(error=list(error_notification_templates), started=list(started_notification_templates), success=list(success_notification_templates))

    '''
    RelatedJobsMixin
    '''

    def _get_related_jobs(self):
        return UnifiedJob.objects.filter(unified_job_template=self)


class Job(UnifiedJob, JobOptions, SurveyJobMixin, JobNotificationMixin, TaskManagerJobMixin, CustomVirtualEnvMixin, WebhookMixin):
    """
    A job applies a project (with playbook) to an inventory source with a given
    credential.  It represents a single invocation of ansible-playbook with the
    given parameters.
    """

    class Meta:
        app_label = 'main'
        ordering = ('id',)

    job_template = models.ForeignKey(
        'JobTemplate',
        related_name='jobs',
        blank=True,
        null=True,
        default=None,
        on_delete=models.SET_NULL,
    )
    hosts = models.ManyToManyField(
        'Host',
        related_name='jobs',
        editable=False,
        through='JobHostSummary',
    )
    artifacts = JSONBlob(
        default=dict,
        blank=True,
        editable=False,
    )
    scm_revision = models.CharField(
        max_length=1024,
        blank=True,
        default='',
        editable=False,
        verbose_name=_('SCM Revision'),
        help_text=_('The SCM Revision from the Project used for this job, if available'),
    )
    project_update = models.ForeignKey(
        'ProjectUpdate',
        blank=True,
        null=True,
        default=None,
        on_delete=models.SET_NULL,
        help_text=_('The SCM Refresh task used to make sure the playbooks were available for the job run'),
    )
    job_slice_number = models.PositiveIntegerField(
        blank=True,
        default=0,
        help_text=_("If part of a sliced job, the ID of the inventory slice operated on. " "If not part of sliced job, parameter is not used."),
    )
    job_slice_count = models.PositiveIntegerField(
        blank=True,
        default=1,
        help_text=_("If ran as part of sliced jobs, the total number of slices. " "If 1, job is not part of a sliced job."),
    )

    def _get_parent_field_name(self):
        return 'job_template'

    @classmethod
    def _get_task_class(cls):
        from awx.main.tasks.jobs import RunJob

        return RunJob

    def _global_timeout_setting(self):
        return 'DEFAULT_JOB_TIMEOUT'

    @classmethod
    def _get_unified_job_template_class(cls):
        return JobTemplate

    def get_absolute_url(self, request=None):
        return reverse('api:job_detail', kwargs={'pk': self.pk}, request=request)

    def get_ui_url(self):
        return urljoin(settings.TOWER_URL_BASE, "/#/jobs/playbook/{}".format(self.pk))

    def _set_default_dependencies_processed(self):
        """
        This sets the initial value of dependencies_processed
        and here we use this as a shortcut to avoid the DependencyManager for jobs that do not need it
        """
        if (not self.project) or self.project.scm_update_on_launch:
            self.dependencies_processed = False
        elif (not self.inventory) or self.inventory.inventory_sources.filter(update_on_launch=True).exists():
            self.dependencies_processed = False
        else:
            # No dependencies to process
            self.dependencies_processed = True

    @property
    def event_class(self):
        if self.has_unpartitioned_events:
            return UnpartitionedJobEvent
        return JobEvent

    def copy_unified_job(self, **new_prompts):
        # Needed for job slice relaunch consistency, do no re-spawn workflow job
        # target same slice as original job
        new_prompts['_prevent_slicing'] = True
        new_prompts.setdefault('_eager_fields', {})
        new_prompts['_eager_fields']['job_slice_number'] = self.job_slice_number
        new_prompts['_eager_fields']['job_slice_count'] = self.job_slice_count
        return super(Job, self).copy_unified_job(**new_prompts)

    def get_passwords_needed_to_start(self):
        return self.passwords_needed_to_start

    def _get_hosts(self, **kwargs):
        Host = JobHostSummary._meta.get_field('host').related_model
        kwargs['job_host_summaries__job__pk'] = self.pk
        return Host.objects.filter(**kwargs)

    def retry_qs(self, status):
        """
        Returns Host queryset that will be used to produce the `limit`
        field in a retry on a subset of hosts
        """
        kwargs = {}
        if status == 'all':
            pass
        elif status == 'failed':
            # Special case for parity with Ansible .retry files
            kwargs['job_host_summaries__failed'] = True
        elif status in ['ok', 'changed', 'unreachable']:
            if status == 'unreachable':
                status_field = 'dark'
            else:
                status_field = status
            kwargs['job_host_summaries__{}__gt'.format(status_field)] = 0
        else:
            raise ParseError(_('{status_value} is not a valid status option.').format(status_value=status))
        return self._get_hosts(**kwargs)

    def _get_task_impact(self):
        if self.launch_type == 'callback':
            count_hosts = 2
        else:
            # If for some reason we can't count the hosts then lets assume the impact as forks
            if self.inventory is not None:
                count_hosts = self.inventory.total_hosts
                if self.job_slice_count > 1:
                    # Integer division intentional
                    count_hosts = (count_hosts + self.job_slice_count - self.job_slice_number) // self.job_slice_count
            else:
                count_hosts = 5 if self.forks == 0 else self.forks
        return min(count_hosts, 5 if self.forks == 0 else self.forks) + 1

    @property
    def successful_hosts(self):
        return self._get_hosts(job_host_summaries__ok__gt=0)

    @property
    def failed_hosts(self):
        return self._get_hosts(job_host_summaries__failures__gt=0)

    @property
    def changed_hosts(self):
        return self._get_hosts(job_host_summaries__changed__gt=0)

    @property
    def dark_hosts(self):
        return self._get_hosts(job_host_summaries__dark__gt=0)

    @property
    def unreachable_hosts(self):
        return self.dark_hosts

    @property
    def skipped_hosts(self):
        return self._get_hosts(job_host_summaries__skipped__gt=0)

    @property
    def processed_hosts(self):
        return self._get_hosts(job_host_summaries__processed__gt=0)

    @property
    def ignored_hosts(self):
        return self._get_hosts(job_host_summaries__ignored__gt=0)

    @property
    def rescued_hosts(self):
        return self._get_hosts(job_host_summaries__rescued__gt=0)

    def notification_data(self, block=5):
        data = super(Job, self).notification_data()
        all_hosts = {}
        # NOTE: Probably related to job event slowness, remove at some point -matburt
        if block and self.status != 'running':
            summaries = self.job_host_summaries.all()
            while block > 0 and not len(summaries):
                time.sleep(1)
                block -= 1
        else:
            summaries = self.job_host_summaries.all()
        for h in self.job_host_summaries.all():
            all_hosts[h.host_name] = dict(
                failed=h.failed,
                changed=h.changed,
                dark=h.dark,
                failures=h.failures,
                ok=h.ok,
                processed=h.processed,
                skipped=h.skipped,
                rescued=h.rescued,
                ignored=h.ignored,
            )
        data.update(
            dict(
                inventory=self.inventory.name if self.inventory else None,
                project=self.project.name if self.project else None,
                playbook=self.playbook,
                credential=getattr(self.machine_credential, 'name', None),
                limit=self.limit,
                extra_vars=self.display_extra_vars(),
                hosts=all_hosts,
            )
        )
        return data

    def _resources_sufficient_for_launch(self):
        return not (self.inventory_id is None or self.project_id is None)

    def display_artifacts(self):
        """
        Hides artifacts if they are marked as no_log type artifacts.
        """
        artifacts = self.artifacts
        if artifacts.get('_ansible_no_log', False):
            return "$hidden due to Ansible no_log flag$"
        return artifacts

    def get_effective_artifacts(self, **kwargs):
        """Return unified job artifacts (from set_stats) to pass downstream in workflows"""
        if isinstance(self.artifacts, dict):
            return self.artifacts
        return {}

    @property
    def is_container_group_task(self):
        return bool(self.instance_group and self.instance_group.is_container_group)

    @property
    def preferred_instance_groups(self):
        # If the user specified instance groups those will be handled by the unified_job.create_unified_job
        # This function handles only the defaults for a template w/o user specification
        selected_groups = []
        for obj_type in ['job_template', 'inventory', 'organization']:
            if getattr(self, obj_type) is not None:
                for instance_group in getattr(self, obj_type).instance_groups.all():
                    selected_groups.append(instance_group)
                if getattr(getattr(self, obj_type), 'prevent_instance_group_fallback', False):
                    break
        if not selected_groups:
            return self.global_instance_groups
        return selected_groups

    def awx_meta_vars(self):
        r = super(Job, self).awx_meta_vars()
        if self.project:
            for name in JOB_VARIABLE_PREFIXES:
                r['{}_project_revision'.format(name)] = self.project.scm_revision
                r['{}_project_scm_branch'.format(name)] = self.project.scm_branch
        if self.scm_branch:
            for name in JOB_VARIABLE_PREFIXES:
                r['{}_job_scm_branch'.format(name)] = self.scm_branch
        if self.job_template:
            for name in JOB_VARIABLE_PREFIXES:
                r['{}_job_template_id'.format(name)] = self.job_template.pk
                r['{}_job_template_name'.format(name)] = self.job_template.name
        return r

    '''
    JobNotificationMixin
    '''

    def get_notification_templates(self):
        if not self.job_template:
            return NotificationTemplate.objects.none()
        return self.job_template.notification_templates

    def get_notification_friendly_name(self):
        return "Job"

    def _get_inventory_hosts(self, only=('name', 'ansible_facts', 'ansible_facts_modified', 'modified', 'inventory_id'), **filters):
        """Return value is an iterable for the relevant hosts for this job"""
        if not self.inventory:
            return []
        host_queryset = self.inventory.hosts.only(*only)
        if filters:
            host_queryset = host_queryset.filter(**filters)
        host_queryset = self.inventory.get_sliced_hosts(host_queryset, self.job_slice_number, self.job_slice_count)
        if isinstance(host_queryset, QuerySet):
            return host_queryset.iterator()
        return host_queryset

    @log_excess_runtime(logger, debug_cutoff=0.01, msg='Job {job_id} host facts prepared for {written_ct} hosts, took {delta:.3f} s', add_log_data=True)
    def start_job_fact_cache(self, destination, log_data, timeout=None):
        self.log_lifecycle("start_job_fact_cache")
        log_data['job_id'] = self.id
        log_data['written_ct'] = 0
        os.makedirs(destination, mode=0o700)

        if timeout is None:
            timeout = settings.ANSIBLE_FACT_CACHE_TIMEOUT
        if timeout > 0:
            # exclude hosts with fact data older than `settings.ANSIBLE_FACT_CACHE_TIMEOUT seconds`
            timeout = now() - datetime.timedelta(seconds=timeout)
            hosts = self._get_inventory_hosts(ansible_facts_modified__gte=timeout)
        else:
            hosts = self._get_inventory_hosts()

        last_filepath_written = None
        for host in hosts:
            filepath = os.sep.join(map(str, [destination, host.name]))
            if not os.path.realpath(filepath).startswith(destination):
                system_tracking_logger.error('facts for host {} could not be cached'.format(smart_str(host.name)))
                continue
            try:
                with codecs.open(filepath, 'w', encoding='utf-8') as f:
                    os.chmod(f.name, 0o600)
                    json.dump(host.ansible_facts, f)
                    log_data['written_ct'] += 1
                    last_filepath_written = filepath
            except IOError:
                system_tracking_logger.error('facts for host {} could not be cached'.format(smart_str(host.name)))
                continue
        # make note of the time we wrote the last file so we can check if any file changed later
        if last_filepath_written:
            return os.path.getmtime(last_filepath_written)
        return None

    @log_excess_runtime(
        logger,
        debug_cutoff=0.01,
        msg='Job {job_id} host facts: updated {updated_ct}, cleared {cleared_ct}, unchanged {unmodified_ct}, took {delta:.3f} s',
        add_log_data=True,
    )
    def finish_job_fact_cache(self, destination, facts_write_time, log_data):
        self.log_lifecycle("finish_job_fact_cache")
        log_data['job_id'] = self.id
        log_data['updated_ct'] = 0
        log_data['unmodified_ct'] = 0
        log_data['cleared_ct'] = 0
        hosts_to_update = []
        for host in self._get_inventory_hosts():
            filepath = os.sep.join(map(str, [destination, host.name]))
            if not os.path.realpath(filepath).startswith(destination):
                system_tracking_logger.error('facts for host {} could not be cached'.format(smart_str(host.name)))
                continue
            if os.path.exists(filepath):
                # If the file changed since we wrote the last facts file, pre-playbook run...
                modified = os.path.getmtime(filepath)
                if (not facts_write_time) or modified > facts_write_time:
                    with codecs.open(filepath, 'r', encoding='utf-8') as f:
                        try:
                            ansible_facts = json.load(f)
                        except ValueError:
                            continue
                        host.ansible_facts = ansible_facts
                        host.ansible_facts_modified = now()
                        hosts_to_update.append(host)
                        system_tracking_logger.info(
                            'New fact for inventory {} host {}'.format(smart_str(host.inventory.name), smart_str(host.name)),
                            extra=dict(
                                inventory_id=host.inventory.id,
                                host_name=host.name,
                                ansible_facts=host.ansible_facts,
                                ansible_facts_modified=host.ansible_facts_modified.isoformat(),
                                job_id=self.id,
                            ),
                        )
                        log_data['updated_ct'] += 1
                else:
                    log_data['unmodified_ct'] += 1
            else:
                # if the file goes missing, ansible removed it (likely via clear_facts)
                host.ansible_facts = {}
                host.ansible_facts_modified = now()
                hosts_to_update.append(host)
                system_tracking_logger.info('Facts cleared for inventory {} host {}'.format(smart_str(host.inventory.name), smart_str(host.name)))
                log_data['cleared_ct'] += 1
            if len(hosts_to_update) > 100:
                self.inventory.hosts.bulk_update(hosts_to_update, ['ansible_facts', 'ansible_facts_modified'])
                hosts_to_update = []
        if hosts_to_update:
            self.inventory.hosts.bulk_update(hosts_to_update, ['ansible_facts', 'ansible_facts_modified'])


class LaunchTimeConfigBase(BaseModel):
    """
    Needed as separate class from LaunchTimeConfig because some models
    use `extra_data` and some use `extra_vars`. We cannot change the API,
    so we force fake it in the model definitions
     - model defines extra_vars - use this class
     - model needs to use extra data - use LaunchTimeConfig
    Use this for models which are SurveyMixins and UnifiedJobs or Templates
    """

    class Meta:
        abstract = True

    # Prompting-related fields that have to be handled as special cases
    inventory = models.ForeignKey(
        'Inventory',
        related_name='%(class)ss',
        blank=True,
        null=True,
        default=None,
        on_delete=models.SET_NULL,
        help_text=_('Inventory applied as a prompt, assuming job template prompts for inventory'),
    )
    # All standard fields are stored in this dictionary field
    # This is a solution to the nullable CharField problem, specific to prompting
    char_prompts = JSONBlob(default=dict, blank=True)

    # Define fields that are not really fields, but alias to char_prompts lookups
    limit = NullablePromptPseudoField('limit')
    scm_branch = NullablePromptPseudoField('scm_branch')
    job_tags = NullablePromptPseudoField('job_tags')
    skip_tags = NullablePromptPseudoField('skip_tags')
    diff_mode = NullablePromptPseudoField('diff_mode')
    job_type = NullablePromptPseudoField('job_type')
    verbosity = NullablePromptPseudoField('verbosity')
    forks = NullablePromptPseudoField('forks')
    job_slice_count = NullablePromptPseudoField('job_slice_count')
    timeout = NullablePromptPseudoField('timeout')

    # NOTE: additional fields are assumed to exist but must be defined in subclasses
    # due to technical limitations
    SUBCLASS_FIELDS = (
        'instance_groups',  # needs a through model defined
        'extra_vars',  # alternates between extra_vars and extra_data
        'credentials',  # already a unified job and unified JT field
        'labels',  # already a unified job and unified JT field
        'execution_environment',  # already a unified job and unified JT field
    )

    def prompts_dict(self, display=False, for_cls=None):
        data = {}
        if for_cls:
            cls = for_cls
        else:
            cls = JobTemplate
        # Some types may have different prompts, but always subset of JT prompts
        for prompt_name in cls.get_ask_mapping().keys():
            try:
                field = self._meta.get_field(prompt_name)
            except FieldDoesNotExist:
                field = None
            if isinstance(field, models.ManyToManyField):
                if not self.pk:
                    continue  # unsaved object can't have related many-to-many
                prompt_values = list(getattr(self, prompt_name).all())
                # Many to manys can't distinguish between None and []
                # Because of this, from a config perspective, we assume [] is none and we don't save [] into the config
                if len(prompt_values) > 0:
                    data[prompt_name] = prompt_values
            elif prompt_name == 'extra_vars':
                if self.extra_vars:
                    extra_vars = {}
                    if display:
                        extra_vars = self.display_extra_vars()
                    else:
                        extra_vars = self.extra_vars
                    # Depending on model, field type may save and return as string
                    if isinstance(extra_vars, str):
                        extra_vars = parse_yaml_or_json(extra_vars)
                    if extra_vars:
                        data['extra_vars'] = extra_vars
                if self.survey_passwords and not display:
                    data['survey_passwords'] = self.survey_passwords
            else:
                prompt_val = getattr(self, prompt_name)
                if prompt_val is not None:
                    data[prompt_name] = prompt_val
        return data


class LaunchTimeConfig(LaunchTimeConfigBase):
    """
    Common model for all objects that save details of a saved launch config
    WFJT / WJ nodes, schedules, and job launch configs (not all implemented yet)
    """

    class Meta:
        abstract = True

    # Special case prompting fields, even more special than the other ones
    extra_data = JSONBlob(default=dict, blank=True)
    survey_passwords = prevent_search(
        JSONBlob(
            default=dict,
            editable=False,
            blank=True,
        )
    )
    # Fields needed for non-unified job / unified JT models, because they are defined on unified models
    credentials = models.ManyToManyField('Credential', related_name='%(class)ss')
    labels = models.ManyToManyField('Label', related_name='%(class)s_labels')
    execution_environment = models.ForeignKey(
        'ExecutionEnvironment',
        null=True,
        blank=True,
        default=None,
        on_delete=polymorphic.SET_NULL,
        related_name='%(class)s_as_prompt',
        help_text="The container image to be used for execution.",
    )

    @property
    def extra_vars(self):
        return self.extra_data

    @extra_vars.setter
    def extra_vars(self, extra_vars):
        self.extra_data = extra_vars

    def display_extra_vars(self):
        """
        Hides fields marked as passwords in survey.
        """
        if hasattr(self, 'survey_passwords') and self.survey_passwords:
            extra_vars = parse_yaml_or_json(self.extra_vars).copy()
            for key, value in self.survey_passwords.items():
                if key in extra_vars:
                    extra_vars[key] = value
            return extra_vars
        else:
            return self.extra_vars

    def display_extra_data(self):
        return self.display_extra_vars()


class JobLaunchConfig(LaunchTimeConfig):
    """
    Historical record of user launch-time overrides for a job
    Not exposed in the API
    Used for relaunch, scheduling, etc.
    """

    class Meta:
        app_label = 'main'

    job = models.OneToOneField(
        'UnifiedJob',
        related_name='launch_config',
        on_delete=models.CASCADE,
        editable=False,
    )

    # Instance Groups needed for non-unified job / unified JT models
    instance_groups = OrderedManyToManyField(
        'InstanceGroup', related_name='%(class)ss', blank=True, editable=False, through='JobLaunchConfigInstanceGroupMembership'
    )

    def has_user_prompts(self, template):
        """
        Returns True if any fields exist in the launch config that are
        not permissions exclusions
        (has to exist because of callback relaunch exception)
        """
        return self._has_user_prompts(template, only_unprompted=False)

    def has_unprompted(self, template):
        """
        returns True if the template has set ask_ fields to False after
        launching with those prompts
        """
        return self._has_user_prompts(template, only_unprompted=True)

    def _has_user_prompts(self, template, only_unprompted=True):
        prompts = self.prompts_dict()
        ask_mapping = template.get_ask_mapping()
        if template.survey_enabled and (not template.ask_variables_on_launch):
            ask_mapping.pop('extra_vars')
            provided_vars = set(prompts.get('extra_vars', {}).keys())
            survey_vars = set(element.get('variable') for element in template.survey_spec.get('spec', {}) if 'variable' in element)
            if (provided_vars and not only_unprompted) or (provided_vars - survey_vars):
                return True
        for field_name, ask_field_name in ask_mapping.items():
            if field_name in prompts and not (getattr(template, ask_field_name) and only_unprompted):
                if field_name == 'limit' and self.job and self.job.launch_type == 'callback':
                    continue  # exception for relaunching callbacks
                return True
        else:
            return False


class JobHostSummary(CreatedModifiedModel):
    """
    Per-host statistics for each job.
    """

    class Meta:
        app_label = 'main'
        unique_together = [('job', 'host_name')]
        verbose_name_plural = _('job host summaries')
        ordering = ('-pk',)

    job = models.ForeignKey(
        'Job',
        related_name='job_host_summaries',
        on_delete=models.CASCADE,
        editable=False,
    )
    host = models.ForeignKey('Host', related_name='job_host_summaries', null=True, default=None, on_delete=models.SET_NULL, editable=False)

    host_name = models.CharField(
        max_length=1024,
        default='',
        editable=False,
    )

    changed = models.PositiveIntegerField(default=0, editable=False)
    dark = models.PositiveIntegerField(default=0, editable=False)
    failures = models.PositiveIntegerField(default=0, editable=False)
    ignored = models.PositiveIntegerField(default=0, editable=False)
    ok = models.PositiveIntegerField(default=0, editable=False)
    processed = models.PositiveIntegerField(default=0, editable=False)
    rescued = models.PositiveIntegerField(default=0, editable=False)
    skipped = models.PositiveIntegerField(default=0, editable=False)
    failed = models.BooleanField(default=False, editable=False, db_index=True)

    def __str__(self):
        host = getattr_dne(self, 'host')
        hostname = host.name if host else 'N/A'
        return '%s changed=%d dark=%d failures=%d ignored=%d ok=%d processed=%d rescued=%d skipped=%s' % (
            hostname,
            self.changed,
            self.dark,
            self.failures,
            self.ignored,
            self.ok,
            self.processed,
            self.rescued,
            self.skipped,
        )

    def get_absolute_url(self, request=None):
        return reverse('api:job_host_summary_detail', kwargs={'pk': self.pk}, request=request)

    def save(self, *args, **kwargs):
        # If update_fields has been specified, add our field names to it,
        # if it hasn't been specified, then we're just doing a normal save.
        if self.host is not None:
            self.host_name = self.host.name
        update_fields = kwargs.get('update_fields', [])
        self.failed = bool(self.dark or self.failures)
        update_fields.append('failed')
        super(JobHostSummary, self).save(*args, **kwargs)


class SystemJobOptions(BaseModel):
    """
    Common fields for SystemJobTemplate and SystemJob.
    """

    SYSTEM_JOB_TYPE = [
        ('cleanup_jobs', _('Remove jobs older than a certain number of days')),
        ('cleanup_activitystream', _('Remove activity stream entries older than a certain number of days')),
        ('cleanup_sessions', _('Removes expired browser sessions from the database')),
        ('cleanup_tokens', _('Removes expired OAuth 2 access tokens and refresh tokens')),
    ]

    class Meta:
        abstract = True

    job_type = models.CharField(
        max_length=32,
        choices=SYSTEM_JOB_TYPE,
        blank=True,
        default='',
    )


class SystemJobTemplate(UnifiedJobTemplate, SystemJobOptions):
    class Meta:
        app_label = 'main'

    @classmethod
    def _get_unified_job_class(cls):
        return SystemJob

    @classmethod
    def _get_unified_job_field_names(cls):
        return ['name', 'description', 'organization', 'job_type', 'extra_vars']

    def get_absolute_url(self, request=None):
        return reverse('api:system_job_template_detail', kwargs={'pk': self.pk}, request=request)

    @property
    def cache_timeout_blocked(self):
        return False

    @property
    def notification_templates(self):
        # TODO: Go through RBAC instead of calling all(). Need to account for orphaned NotificationTemplates
        base_notification_templates = NotificationTemplate.objects.all()
        error_notification_templates = list(base_notification_templates.filter(unifiedjobtemplate_notification_templates_for_errors__in=[self]))
        started_notification_templates = list(base_notification_templates.filter(unifiedjobtemplate_notification_templates_for_started__in=[self]))
        success_notification_templates = list(base_notification_templates.filter(unifiedjobtemplate_notification_templates_for_success__in=[self]))
        return dict(error=list(error_notification_templates), started=list(started_notification_templates), success=list(success_notification_templates))

    def _accept_or_ignore_job_kwargs(self, _exclude_errors=None, **kwargs):
        extra_data = kwargs.pop('extra_vars', {})
        prompted_data, rejected_data, errors = super(SystemJobTemplate, self)._accept_or_ignore_job_kwargs(**kwargs)
        prompted_vars, rejected_vars, errors = self.accept_or_ignore_variables(extra_data, errors, _exclude_errors=_exclude_errors)
        if prompted_vars:
            prompted_data['extra_vars'] = prompted_vars
        if rejected_vars:
            rejected_data['extra_vars'] = rejected_vars
        return (prompted_data, rejected_data, errors)

    def _accept_or_ignore_variables(self, data, errors, _exclude_errors=()):
        """
        Unlike other templates, like project updates and inventory sources,
        system job templates can accept a limited number of fields
        used as options for the management commands.
        """
        rejected = {}
        allowed_vars = set(['days', 'older_than', 'granularity'])
        given_vars = set(data.keys())
        unallowed_vars = given_vars - (allowed_vars & given_vars)
        errors_list = []
        if unallowed_vars:
            errors_list.append(_('Variables {list_of_keys} are not allowed for system jobs.').format(list_of_keys=', '.join(unallowed_vars)))
            for key in unallowed_vars:
                rejected[key] = data.pop(key)

        if self.job_type in ('cleanup_jobs', 'cleanup_activitystream'):
            if 'days' in data:
                try:
                    if isinstance(data['days'], (bool, type(None))):
                        raise ValueError
                    if float(data['days']) != int(data['days']):
                        raise ValueError
                    days = int(data['days'])
                    if days < 0:
                        raise ValueError
                except ValueError:
                    errors_list.append(_("days must be a positive integer."))
                    rejected['days'] = data.pop('days')

        if errors_list:
            errors['extra_vars'] = errors_list
        return (data, rejected, errors)


class SystemJob(UnifiedJob, SystemJobOptions, JobNotificationMixin):
    class Meta:
        app_label = 'main'
        ordering = ('id',)

    system_job_template = models.ForeignKey(
        'SystemJobTemplate',
        related_name='jobs',
        blank=True,
        null=True,
        default=None,
        on_delete=models.SET_NULL,
    )

    extra_vars = prevent_search(
        models.TextField(
            blank=True,
            default='',
        )
    )

    extra_vars_dict = VarsDictProperty('extra_vars', True)

    def _set_default_dependencies_processed(self):
        self.dependencies_processed = True

    @classmethod
    def _get_parent_field_name(cls):
        return 'system_job_template'

    @classmethod
    def _get_task_class(cls):
        from awx.main.tasks.jobs import RunSystemJob

        return RunSystemJob

    def websocket_emit_data(self):
        return {}

    def get_absolute_url(self, request=None):
        return reverse('api:system_job_detail', kwargs={'pk': self.pk}, request=request)

    def get_ui_url(self):
        return urljoin(settings.TOWER_URL_BASE, "/#/jobs/system/{}".format(self.pk))

    @property
    def event_class(self):
        if self.has_unpartitioned_events:
            return UnpartitionedSystemJobEvent
        return SystemJobEvent

    def _get_task_impact(self):
        return 5

    @property
    def preferred_instance_groups(self):
        return self.control_plane_instance_group

    '''
    JobNotificationMixin
    '''

    def get_notification_templates(self):
        return self.system_job_template.notification_templates

    def get_notification_friendly_name(self):
        return "System Job"
