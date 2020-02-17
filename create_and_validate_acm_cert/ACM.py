import boto3
from botocore.config import Config
import tldextract
from . import aws_helpers
import time


class DNSValidatedACMCertClient():

    def __init__(self, domain, profile='default', region='us-east-1', session=None, acm_client=None, route_53_client=None):
        self.session = session or boto3.Session(profile_name=profile, region_name=region)
        self.acm_client = acm_client or self.session.client('acm')
        self.route_53_client = route_53_client or self.session.client('route53', config=Config(retries={
            'max_attempts': 10}))
        self.list_hosted_zones_paginator = self.route_53_client.get_paginator(
        'list_hosted_zones')
        self.route53_zones = self.list_hosted_zones_paginator.paginate().build_full_result()
        self.domain = domain

    def get_certificate_arn(self, response):
        """ Given an ACM Boto response,
            return the ACM Certificate ARN
        """
        return response.get('CertificateArn')

    def request_certificate(self, subject_alternative_names=[]):
        """ Given a list of (optional) subject alternative names,
            request a certificate and return the certificate ARN.
        """
        if len(subject_alternative_names) > 0:
            response = self.acm_client.request_certificate(
                DomainName=self.domain,
                ValidationMethod='DNS',
                SubjectAlternativeNames=subject_alternative_names)
        else:
            response = self.acm_client.request_certificate(
                DomainName=self.domain, ValidationMethod='DNS')

        if aws_helpers.response_succeeded(response):
            return self.get_certificate_arn(response)

    def get_certificate_status(self, certificate_arn):
        return self.acm_client.describe_certificate(CertificateArn=certificate_arn)['Certificate']['Status']

    def wait_for_certificate_validation(self, certificate_arn, sleep_time=5, timeout=600):

        status = self.get_certificate_status(certificate_arn)
        elapsed_time = 0
        while status == 'PENDING_VALIDATION':
            if elapsed_time > timeout:
                raise Exception('Timeout ({}s) reached for certificate validation'.format(timeout))
            print("{}: Waiting {}s for validation, {}s elapsed...".format(certificate_arn, sleep_time, elapsed_time))
            time.sleep(sleep_time)
            status = self.get_certificate_status(certificate_arn)
            elapsed_time += sleep_time

    def get_domain_validation_records(self, arn):
        """ Return the domain validation records from the describe_certificate
            call for our certificate
        """
        certificate_metadata = self.acm_client.describe_certificate(
            CertificateArn=arn)
        return certificate_metadata.get('Certificate', {}).get(
            'DomainValidationOptions', [])

    def get_hosted_zone_id(self, validation_dns_record):
        """ Return the HostedZoneId of the zone tied to the root domain
            of the domain the user wants to protect (e.g. given www.cnn.com, return cnn.com)
            if it exists in Route53. Else error.
        """

        def get_domain_from_host(validation_dns_record):
            """ Given an FQDN, return the domain
                portion of a host
            """
            domain_tld_info = tldextract.extract(validation_dns_record)
            return "%s.%s" % (domain_tld_info.domain, domain_tld_info.suffix)

        def domain_matches_hosted_zone(domain, zone):
            return zone.get('Name') == "%s." % (domain)

        def get_zone_id_from_id_string(zone_id_string):
            return zone_id_string.split('/')[-1]

        hosted_zone_domain = get_domain_from_host(validation_dns_record)

        target_record = list(
            filter(
                lambda zone: domain_matches_hosted_zone(hosted_zone_domain, zone),
                self.route53_zones.get('HostedZones')))

        return get_zone_id_from_id_string(target_record[0].get('Id'))

    def get_resource_record_data(self, r):
        """ Given a ResourceRecord dictionary from an ACM certificate response,
            return the type, name and value of the record
        """
        return (r.get('Type'), r.get('Name'), r.get('Value'))

    def create_dns_record_set(self, record):
        """ Given a HostedZoneId and a list of domain validation records,
            create a DNS record set to send to Route 53
        """
        record_type, record_name, record_value = self.get_resource_record_data(
            record.get('ResourceRecord'))
        print("Creating %s record for %s" % (record_type, record_name))

        return {
            'Action': 'UPSERT',
            'ResourceRecordSet': {
                'Name': record_name,
                'Type': record_type,
                'ResourceRecords': [{
                    'Value': record_value
                }],
                'TTL': 300,
            }
        }

    def remove_duplicate_upsert_records(self, original_list):
        unique_list = []
        [unique_list.append(obj) for obj in original_list if obj not in unique_list]
        return unique_list

    def create_domain_validation_records(self, arn):
        """ Given an ACM certificate ARN,
            return the response
        """
        domain_validation_records = self.get_domain_validation_records(arn)

        changes = [
            self.create_dns_record_set(record)
            for record in domain_validation_records
        ]
        unique_changes = self.remove_duplicate_upsert_records(changes)
        for change in unique_changes:
            record_name = change.get('ResourceRecordSet').get('Name')
            hosted_zone_id = self.get_hosted_zone_id(record_name)
            response = self.route_53_client.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                'Changes': [change]
            })

            if aws_helpers.response_succeeded(response):
                print("Successfully created Route 53 record set for {}".format(record_name))
            else:
                print("Failed to create Route53 record set: {}".format(response))
