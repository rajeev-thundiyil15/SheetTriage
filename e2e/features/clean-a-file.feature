Feature: Clean a messy CSV
  A visitor uploads a file, confirms the drafted schema, and reviews what the agent fixed and flagged.

  Scenario: A visitor tries the demo data
    Given I am on the upload page
    When I try the demo data
    Then the schema page reads "event_registrations.csv: 40 rows, 9 columns"
    And the date question suggests "Month first"
    When I run the agent
    Then the run finishes
    And the summary ends with "of 40 rows are clean."

  Scenario: A value that breaks a rule is flagged, not changed
    Given I am on the upload page
    When I upload "orders.csv"
    And I run the agent
    Then the run finishes
    And the summary reads "9 of 10 rows are clean."
    And 1 cell is flagged, showing "not-an-email"

  Scenario Outline: A file that can't be read safely is refused
    Given I am on the upload page
    When I upload "<file>"
    Then I stay on the upload page with the message "<message>"

    Examples:
      | file             | message                                                                                                               |
      | extra-values.csv | Row 3 has 3 values but the header has 2 columns, so its values can't be matched to columns. Fix that row and upload again. |
