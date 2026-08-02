---
name: Bug report
about: Create a report to help us improve
title: "[BUG]"
labels: ''
assignees: ''

---

name: "🐞 Bug"
description: "버그 수정"
title: "[Bug] "
labels: ["bug"]
body:
  - type: textarea
    id: summary
    attributes:
      label: 버그 설명
      placeholder: 어떤 문제가 발생했나요?
    validations:
      required: true

  - type: textarea
    id: reproduce
    attributes:
      label: 재현 방법
      value: |
        1. 
        2. 
    validations:
      required: true

  - type: textarea
    id: expected
    attributes:
      label: 예상 동작
      placeholder: 원래 어떻게 동작해야 하나요?
    validations:
      required: true
