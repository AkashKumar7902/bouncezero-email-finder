# Cold-outreach audit — email-pattern & provider knowledge base

- Recipient records analyzed: **3006**
- Distinct domains: **219**
- Distinct companies: **210**

## Global local-part shape distribution
| shape | count | pct |
|---|---|---|
| first.last | 2313 | 76% |
| single_token | 421 | 14% |
| first.l | 200 | 6% |
| first_last | 22 | 0% |
| name+digits | 20 | 0% |
| f.last | 19 | 0% |
| first_l | 5 | 0% |
| multi. | 3 | 0% |
| first-last | 2 | 0% |
| other | 1 | 0% |

## Global separator usage
| separator | count |
|---|---|
| . | 2535 |
| (none) | 442 |
| _ | 27 |
| - | 2 |

## Bounce reason classes (all bounces)
| reason_class | count |
|---|---|
| address_not_found | 352 |
| recipient_rejected | 134 |
| routing_loop | 20 |
| dns_failure | 17 |
| policy_or_spam_rejection | 13 |
| inactive_account | 10 |
| connection_failure | 10 |
| group_not_found_or_permission_denied | 3 |
| connection_failure; temporary_failure | 1 |

## Provider behavior (KEY INSIGHT for verification reliability)
Reject/verify behavior differs sharply by mail provider. `recipient_rejected` (550 5.4.1 Access denied) on Microsoft 365 is a **policy block that fires even for valid mailboxes**, so an SMTP RCPT probe cannot confirm/deny those addresses. `address_not_found` (550 5.1.1) is a **true negative** — that mailbox does not exist.

| provider | domains | addrs | addr_not_found | recip_rejected | inactive | no_bounce |
|---|---|---|---|---|---|---|
| google_workspace | 114 | 990 | 175 | 10 | 10 | 747 |
| microsoft365 | 46 | 810 | 3 | 118 | 0 | 653 |
| proofpoint | 25 | 715 | 77 | 0 | 0 | 638 |
| cisco_ironport | 8 | 216 | 52 | 3 | 0 | 159 |
| other | 15 | 139 | 29 | 3 | 0 | 107 |
| mimecast | 6 | 123 | 15 | 0 | 0 | 108 |
| none_or_unknown | 3 | 11 | 1 | 0 | 0 | 0 |
| zoho | 2 | 2 | 0 | 0 | 0 | 2 |

## Top 30 domains by volume
| domain | company | addrs | provider | dominant shape | addr_not_found | rejected | no_bounce |
|---|---|---|---|---|---|---|---|
| amadeus.com | Amadeus | 209 | microsoft365 | first.last | 0 | 0 | 209 |
| harman.com | Harman | 104 | cisco_ironport | first.last | 39 | 3 | 62 |
| ukg.com | UKG | 86 | mimecast | first.last | 9 | 0 | 77 |
| experian.com | Experian | 85 | proofpoint | first.last | 0 | 0 | 85 |
| chargebee.com | Chargebee | 75 | google_workspace | first.last | 21 | 0 | 54 |
| booking.com | Booking Holdings | 69 | proofpoint | first.last | 0 | 0 | 69 |
| ge.com | GE Vernova | 61 | proofpoint | first.last | 10 | 0 | 51 |
| opengov.com | OpenGov | 60 | proofpoint | single_token | 0 | 0 | 60 |
| honeywell.com | Honeywell | 58 | microsoft365 | first.last | 0 | 0 | 58 |
| ingrammicro.com | Ingram Micro | 58 | proofpoint | first.last | 0 | 0 | 58 |
| simcorp.com | SimCorp | 53 | microsoft365 | first.last | 0 | 3 | 50 |
| cdk.com | CDK | 52 | microsoft365 | first.last | 0 | 0 | 38 |
| capco.com | Capco | 51 | microsoft365 | first.last | 0 | 24 | 27 |
| wingify.com | Wingify | 51 | google_workspace | first.last | 0 | 0 | 41 |
| navi.com | Navi | 50 | google_workspace | first.last | 5 | 0 | 43 |
| akamai.com | Akamai Technologies | 48 | proofpoint | first.last | 6 | 0 | 42 |
| healthedge.com | HealthEdge | 48 | microsoft365 | first.last | 0 | 14 | 34 |
| target.com | Target | 48 | proofpoint | first.last | 0 | 0 | 48 |
| aciworldwide.com | ACI Worldwide | 44 | microsoft365 | first.last | 0 | 10 | 34 |
| metlife.com | MetLife | 44 | cisco_ironport | first.last | 0 | 0 | 43 |
| siemens-healthineers.com | Siemens Healthineers | 42 | microsoft365 | first.last | 0 | 7 | 35 |
| tessell.com | Tessell | 41 | google_workspace | first.last | 9 | 0 | 32 |
| easebuzz.in | Easebuzz | 40 | google_workspace | first.last | 7 | 0 | 33 |
| entainindia.com | Entain India | 39 | microsoft365 | first.last | 0 | 20 | 19 |
| invesco.com | Invesco | 39 | proofpoint | first.last | 18 | 0 | 21 |
| soti.net | SOTI | 39 | cisco_ironport | first.last | 0 | 0 | 39 |
| h2o.ai | H2O.ai | 38 | google_workspace | first.last | 7 | 0 | 31 |
| gmail.com | Amadeus | 36 | google_workspace | name+digits | 0 | 0 | 36 |
| purplle.com | Purplle | 34 | google_workspace | first.l | 4 | 0 | 28 |
| alaan.com | Alaan | 32 | google_workspace | first.last | 0 | 0 | 0 |
